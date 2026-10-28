def test_mfa_seed_is_delivered_once_never_returned_in_audit(client, headers):
    response = client.get("/api/security/enrollment", headers=headers)
    assert response.status_code == 200
    seed = response.json()["manual_secret"]
    assert len(seed) >= 32
    assert client.get("/api/security/enrollment", headers=headers).status_code == 409
    audit = client.get("/api/audit", headers=headers)
    assert seed not in audit.text and audit.json()["chain_verified"]
