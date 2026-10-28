import os
from pathlib import Path

import numpy as np
import pytest

from bank_agent.config import Settings
from bank_agent.memory import Embeddings


@pytest.mark.embedding_model
@pytest.mark.skipif(
    os.getenv("BANK_TEST_EMBEDDING") != "1", reason="Prepare the local model and set BANK_TEST_EMBEDDING=1"
)
def test_real_chinese_embeddings_load_from_cache_and_retrieve_semantic_matches():
    provider = Embeddings(Settings(data_dir=Path(".data").resolve(), memory_embedding="fastembed"))
    vectors = provider.encode(["查询账户余额", "核对银行卡可用资金", "生日鲜花配送"])
    query = provider.encode(["账户里还有多少钱"], query=True)[0]
    assert provider.backend == "fastembed", provider.error
    assert vectors.shape[0] == 3 and vectors.shape[1] >= 128
    assert np.isfinite(vectors).all() and np.allclose(np.linalg.norm(vectors, axis=1), 1, atol=1e-5)
    scores = vectors @ query
    assert max(scores[:2]) > scores[2]
