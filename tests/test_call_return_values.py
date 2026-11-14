from wake_rs import Chain

RUNTIME = bytes.fromhex("602a60005260206000f3")  # MSTORE(0, 42), RETURN(0, 32)
INIT = bytes.fromhex("600a600c600039600a6000f3") + RUNTIME  # copy RUNTIME, return it


def test_deploy_call_returns_runtime_code_on_revm():
    chain = Chain()
    with chain.connect(accounts=1):
        call = chain.deploy(INIT, request_type="call", return_call=True)

        # A call deploys nothing: its raw value is the returned runtime code, as
        # on JSON-RPC nodes, not the address the contract would be deployed at.
        assert call.raw_return_value == RUNTIME
        assert call.return_value == RUNTIME
        assert chain.deploy(INIT, request_type="call") == RUNTIME

        # A transaction does deploy, so its raw value stays the new account.
        tx = chain.deploy(INIT, return_tx=True)
        assert tx.raw_return_value.code == RUNTIME
