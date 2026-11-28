"""CORE-7 ModelProfile / ModelClient and X7 configuration."""
import pytest

from nai.config import ConfigError, load_config
from nai.models import ModelProfile, RoleConfig, default_profile
from nai.testing.stub_server import reply
from tests.core.conftest import make_client

TOML = """
default_profile = "fast"
[server]
base_url = "http://example:9/v1"
api_key = "fromfile"
timeout = 5
stream = false
swap_threshold_seconds = 3
[profiles.fast]
swap_cost_seconds = 25
[profiles.fast.reasoner]
model_id = "rr"
ctx = 8192
max_tokens = 2000
temperature = 0.2
top_p = 0.9
top_k = 10
[profiles.fast.coder]
model_id = "cc"
repeat_penalty = 1.1
min_p = 0.05
"""


def write(tmp_path, text=TOML):
    p = tmp_path / "nai.toml"
    p.write_text(text)
    return p


def test_default_profile_matches_v1_constants():
    p = default_profile()
    assert (p.reasoner.model_id, p.coder.model_id) == ("qwen3.6-reasoning", "qwen3-coder")
    assert p.reasoner.sampling() == dict(temperature=0.6, top_p=0.95, top_k=20)
    assert p.coder.sampling() == dict(temperature=0.7, top_p=0.8, top_k=20, repeat_penalty=1.05)
    assert (p.reasoner.ctx, p.reasoner.max_tokens) == (32768, 16384)


def test_profile_roundtrip_and_unknown_keys_are_extras():
    rc = RoleConfig.from_dict({"model_id": "m", "temperature": 0.1, "top_p": 0.5, "min_p": 0.02})
    assert rc.extras == {"min_p": 0.02} and rc.sampling()["min_p"] == 0.02
    prof = ModelProfile("x", rc, rc, 12.0)
    assert ModelProfile.from_dict("x", prof.to_dict()) == prof


def test_profile_errors():
    with pytest.raises(ValueError):
        RoleConfig.from_dict({"ctx": 1})
    with pytest.raises(ValueError):
        ModelProfile.from_dict("x", {"reasoner": {"model_id": "a"}})
    with pytest.raises(ValueError):
        default_profile().role("judge")


def test_load_toml(tmp_path):
    cfg = load_config(write(tmp_path), env={})
    assert cfg.server.base_url == "http://example:9/v1" and cfg.server.stream is False and cfg.server.swap_threshold_seconds == 3
    p = cfg.get_profile(env={})
    assert p.name == "fast" and p.swap_cost_seconds == 25 and p.reasoner.ctx == 8192
    assert p.coder.extras == {"repeat_penalty": 1.1, "min_p": 0.05}


def test_env_overrides_file_and_args_override_env(tmp_path):
    env = {"LLAMA_SWAP_URL": "http://env/v1", "LLAMA_SWAP_KEY": "envkey", "REASONER_MODEL": "env-r", "CODER_MODEL": "env-c"}
    cfg = load_config(write(tmp_path), env=env)
    assert (cfg.server.base_url, cfg.server.api_key) == ("http://env/v1", "envkey")
    p = cfg.get_profile(env=env)
    assert (p.reasoner.model_id, p.coder.model_id) == ("env-r", "env-c")
    p = cfg.get_profile(reasoner="arg-r", ctx=999, max_tokens=5, env=env)
    assert (p.reasoner.model_id, p.coder.model_id, p.reasoner.ctx, p.coder.ctx, p.reasoner.max_tokens, p.coder.max_tokens) == \
        ("arg-r", "env-c", 999, 32768, 5, 5)


def test_no_file_gives_defaults(tmp_path):
    cfg = load_config(env={}, cwd=tmp_path)
    assert cfg.server.base_url == "http://localhost:8080/v1" and cfg.get_profile(env={}).name == "default"


def test_discovery_order(tmp_path):
    (tmp_path / "nai.toml").write_text('[server]\napi_key = "cwd"\n')
    other = tmp_path / "other.toml"
    other.write_text('[server]\napi_key = "explicit"\n')
    assert load_config(env={}, cwd=tmp_path).server.api_key == "cwd"
    assert load_config(env={"NAI_CONFIG": str(other)}, cwd=tmp_path).server.api_key == "explicit"
    assert load_config(other, env={"NAI_CONFIG": "/nope"}, cwd=tmp_path).server.api_key == "explicit"


@pytest.mark.parametrize("text", ['[server]\nbogus = 1\n', 'x = = 1', '[profiles.p.reasoner]\nmodel_id="a"\n'])
def test_config_errors(tmp_path, text):
    with pytest.raises(ConfigError):
        load_config(write(tmp_path, text), env={})


def test_missing_explicit_file_and_unknown_profile(tmp_path):
    with pytest.raises(ConfigError):
        load_config(tmp_path / "missing.toml", env={})
    with pytest.raises(ConfigError):
        load_config(env={}, cwd=tmp_path).get_profile("nope", env={})


def test_modelclient_sampling_override_and_roles(stub):
    stub.scripts["consult"] = [reply("a")]
    c = make_client(stub)
    assert c.model_id("reasoner") == "r" and c.ctx("coder") == 32768 and c.max_tokens("reasoner") == 1000
    assert c.sampling("reasoner", temperature=1.0)["temperature"] == 1.0
    c.chat("reasoner", [{"role": "user", "content": "x"}], sampling={"temperature": 0.9}, max_tokens=5)
    r = stub.requests[0]
    assert (r["model"], r["temperature"], r["top_p"], r["max_tokens"]) == ("r", 0.9, 0.95, 5)
    with pytest.raises(ValueError):
        c.chat("judge", [])
