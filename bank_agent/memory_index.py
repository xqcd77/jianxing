"""Rebuildable Chroma index. SQL source records remain the access/identity authority."""

from datetime import datetime
import hashlib
from pathlib import Path
from threading import RLock
import time
from urllib.parse import urlsplit

from .errors import BankError


_clients = {}
_clients_lock = RLock()


class ChromaMemoryIndex:
    def __init__(self, data_dir, server_url=""):
        self.path = str((Path(data_dir) / "chroma").resolve())
        self.server = urlsplit(server_url) if server_url else None
        if self.server and (self.server.scheme not in {"http", "https"} or not self.server.hostname
                            or self.server.username or self.server.password or self.server.query
                            or self.server.fragment or self.server.path not in {"", "/"}):
            raise BankError("MEMORY_CONFIG_INVALID", "Chroma地址配置无效", 503)
        self.key = "server:" + server_url.rstrip("/") if self.server else self.path
        self._client = None
        self._lock = RLock()
        self._known = {}
        self._deleted = set()

    def _collection(self, model_id, dimension):
        key = hashlib.sha256((model_id + ':' + str(dimension)).encode()).hexdigest()[:24]
        return self._open().get_or_create_collection(name='bank_memory_'+key,embedding_function=None,
            configuration={'hnsw':{'space':'cosine'}})

    def upsert(self, rows, vectors, model_id, tags):
        if not rows:
            return
        with self._lock:
            collection = self._collection(model_id,len(vectors[0]))
            metadata = [self._metadata(row,tag) for row,tag in zip(rows,tags,strict=True)]
            changed = [i for i,row in enumerate(rows) if row.id not in self._deleted
                       and self._known.get((collection.name,row.id)) != metadata[i]]
            if changed:
                collection.upsert(ids=[rows[i].id for i in changed],embeddings=[vectors[i].tolist() for i in changed],
                    metadatas=[metadata[i] for i in changed])
                for i in changed:
                    self._known[(collection.name,rows[i].id)] = metadata[i]

    def _open(self):
        with _clients_lock:
            if self._client is None:
                if self.key not in _clients:
                    import chromadb
                    from chromadb.config import Settings

                    config = Settings(anonymized_telemetry=False)
                    if self.server:
                        client = chromadb.HttpClient(
                            host=self.server.hostname, port=self.server.port or (443 if self.server.scheme == "https" else 80),
                            ssl=self.server.scheme == "https", settings=config,
                        )
                    else:
                        client = chromadb.PersistentClient(path=self.path, settings=config)
                    _clients[self.key] = [client, 0]
                self._client = _clients[self.key][0]
                _clients[self.key][1] += 1
            return self._client

    @staticmethod
    def _metadata(record, tag):
        return {
            "memory_id": record.id, "owner_id": record.owner_id,
            "conversation_id": record.conversation_id, "scope": record.scope,
            "expires_at_ts": datetime.fromisoformat(record.expires_at).timestamp(),
            "source_id": record.source_id, "source_type": record.source_type,
            "version": record.version, "index_tag": tag,
            **{"allow_" + role: role in record.roles
               for role in ("funds", "bills", "wealth", "cards", "subscriptions", "life")},
        }

    def rank(self, rows, vectors, query_vector, model_id, tags, user_id, role, conversation_id, limit):
        if not rows:
            return []
        # Stable per-model/per-dimension collections prevent mixing incompatible vectors.
        try:
            with self._lock:
                collection = self._collection(model_id,len(query_vector))
                self.upsert(rows,vectors,model_id,tags)  # Cold repair only; normal writes prepare the index asynchronously.
                ids = [r.id for r in rows if r.id not in self._deleted]
                if not ids:
                    return []
                # These constraints are built by the service from trusted auth state.
                conditions = [
                    {"owner_id": user_id}, {"memory_id": {"$in": ids}},
                    {"expires_at_ts": {"$gt": time.time()}},
                    {"$or": [{"scope": "user"}, {"$and": [
                        {"scope": "conversation"}, {"conversation_id": conversation_id or ""},
                    ]}]},
                ]
                if role not in {"planner", "reviewer"}:
                    conditions.append({"allow_" + role: True})
                result = collection.query(
                    query_embeddings=[query_vector.tolist()], n_results=min(limit, len(ids)),
                    where={"$and": conditions}, include=["distances"],
                )
                # Never trust a returned document/metadata as original evidence.
                allowed = {row.id: row for row in rows}
                return [(allowed[record_id], 1.0 - float(distance))
                        for record_id, distance in zip(result["ids"][0], result["distances"][0], strict=True)
                        if record_id in allowed]
        except BankError:
            raise
        except Exception as error:
            raise BankError("MEMORY_INDEX_UNAVAILABLE", "记忆检索服务暂不可用，请稍后重试", 503) from error

    def delete(self, ids):
        if not ids or (not self.server and self._client is None and not Path(self.path).exists()):
            return
        try:
            with self._lock:
                client = self._open()
                for collection in client.list_collections():
                    name = collection.name if hasattr(collection, "name") else collection
                    if name.startswith("bank_memory_"):
                        client.get_collection(name, embedding_function=None).delete(ids=ids)
                self._deleted.update(ids)
        except Exception as error:
            raise BankError("MEMORY_INDEX_UNAVAILABLE", "记忆索引尚未完成删除，请稍后重试", 503) from error

    def close(self):
        with self._lock, _clients_lock:
            if self._client is None:
                return
            entry = _clients[self.key]
            entry[1] -= 1
            if entry[1] == 0:
                self._client.close()
                _clients.pop(self.key)
            self._client = None
