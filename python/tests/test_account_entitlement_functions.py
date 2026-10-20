"""The owner's key script and the Edge Function code (TypeScript, run here with Node) against the
Python engine: a token signed by the function logic verifies in the engine, and the key script's
public key and ``kid`` match what the function derives from the private key. Skipped when Node is
too old to run TypeScript files directly (the functions' own tests need Node 22.18 or newer)."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import time
from pathlib import Path

import pytest

from engine.account import tokens
from tests.account_helpers import run_python

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "scripts" / "make_entitlement_key.py"
FUNCTIONS = REPO / "supabase" / "functions"


def node_runs_typescript() -> str | None:
    node = shutil.which("node")
    if node is None:
        return None
    out = subprocess.run([node, "--version"], capture_output=True, text=True, close_fds=False)
    match = re.match(r"v(\d+)\.(\d+)", out.stdout)
    if not match:
        return None
    major, minor = int(match[1]), int(match[2])
    return node if (major, minor) >= (22, 18) else None


needs_node = pytest.mark.skipif(node_runs_typescript() is None, reason="Node >= 22.18 not found")


def make_key(tmp_path: Path):
    out = tmp_path / "entitlement-private.pem"
    proc = run_python([str(SCRIPT), "--out", str(out)], env={"PATH": "/usr/bin:/bin"})
    assert proc.returncode == 0, proc.stderr
    return out, proc


def public_jwk(stdout: str) -> dict:
    start = stdout.index("{")
    end = stdout.index("\n}", start) + 2
    return json.loads(stdout[start:end])


def test_the_key_script_writes_a_private_file_and_prints_only_the_public_key(tmp_path):
    out, proc = make_key(tmp_path)
    assert oct(out.stat().st_mode & 0o777) == "0o600"
    pem = out.read_text()
    assert pem.startswith("-----BEGIN PRIVATE KEY-----")
    assert "PRIVATE KEY" not in proc.stdout and "PRIVATE KEY" not in proc.stderr
    body = "".join(pem.splitlines()[1:-1])
    assert body not in proc.stdout.replace("\n", "")
    jwk = public_jwk(proc.stdout)
    assert jwk["kty"] == "EC" and jwk["crv"] == "P-256" and "d" not in jwk
    assert jwk["kid"] and jwk["alg"] == "ES256"
    assert "supabase secrets set --env-file" in proc.stdout
    assert str(out) in proc.stdout  # the path, never the content


def test_the_key_script_refuses_to_overwrite_or_to_write_inside_the_repository(tmp_path):
    out, _ = make_key(tmp_path)
    again = run_python([str(SCRIPT), "--out", str(out)], env={"PATH": "/usr/bin:/bin"})
    assert again.returncode == 2 and "overwrite" in again.stderr
    inside = run_python(
        [str(SCRIPT), "--out", str(REPO / "oops-entitlement-private.pem")],
        env={"PATH": "/usr/bin:/bin"},
    )
    assert inside.returncode == 2 and "must not be written inside a git repository" in inside.stderr
    assert not (REPO / "oops-entitlement-private.pem").exists()


def test_two_runs_make_two_different_keys(tmp_path):
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    a = public_jwk(make_key(tmp_path / "a")[1].stdout)
    b = public_jwk(make_key(tmp_path / "b")[1].stdout)
    assert a["kid"] != b["kid"] and a["x"] != b["x"]


def test_the_repository_contains_no_private_key():
    """No PEM private key body in any tracked or new file (the key script prints none either)."""
    git = shutil.which(
        "git"
    )  # a full path: only then can subprocess use posix_spawn (see run_python)
    if git is None:
        pytest.skip("git not found")
    listing = subprocess.run(
        [git, "-C", str(REPO), "ls-files", "-co", "--exclude-standard"],
        capture_output=True,
        text=True,
        close_fds=False,
    )
    if listing.returncode != 0:
        pytest.skip("not a git checkout")
    pem = re.compile(r"-----BEGIN (?:[A-Z]+ )?PRIVATE KEY-----\s*[A-Za-z0-9+/=\s]{60,}")
    for name in listing.stdout.splitlines():
        path = REPO / name
        if path.is_file() and path.stat().st_size < 3_000_000:
            assert not pem.search(path.read_text(errors="ignore")), name


NODE_SIGN = """
import { readFileSync } from 'node:fs';
import { loadSigningKey } from '%(entitlement)s';
import { handleExchange } from '%(exchange)s';
import { signEntitlement, sha256Hex } from '%(entitlement)s';
const key = await loadSigningKey(readFileSync(process.argv[2], 'utf8'));
const token = process.argv[3];
const now = Math.floor(Date.now() / 1000);
const rows = new Map([[await sha256Hex(token), {
  id: '00000000-0000-0000-0000-000000000001', user_id: '11111111-2222-3333-4444-555555555555',
  email: 'ada@example.test', name: 'from-node', expires_at: new Date((now + 40 * 86400) * 1000).toISOString(),
}]]);
const out = await handleExchange({ method: 'POST', body: { token }, clientAddress: '203.0.113.5' }, {
  lookup: async (h) => rows.get(h) ?? null,
  rateHit: async () => true,
  rateCheck: async () => true,
  sign: (claims) => signEntitlement(claims, key),
  nowSeconds: () => now,
  newId: () => crypto.randomUUID(),
});
console.log(JSON.stringify({ kid: key.kid, out }));
"""


@needs_node
def test_a_token_from_the_function_logic_verifies_in_the_engine(tmp_path, monkeypatch):
    from tests.account_helpers import new_pat

    pem_path, proc = make_key(tmp_path)
    jwk = public_jwk(proc.stdout)
    script = tmp_path / "sign.mjs"
    script.write_text(
        NODE_SIGN
        % {
            "entitlement": (FUNCTIONS / "_shared" / "entitlement.ts").as_posix(),
            "exchange": (FUNCTIONS / "exchange-api-token" / "exchange.ts").as_posix(),
        }
    )
    pat = new_pat()
    node = node_runs_typescript()
    done = subprocess.run(
        [node, str(script), str(pem_path), pat], capture_output=True, text=True, close_fds=False
    )
    assert done.returncode == 0, done.stderr
    result = json.loads(done.stdout.strip().splitlines()[-1])
    assert result["kid"] == jwk["kid"], "the function derives the same kid the key script prints"
    assert result["out"]["status"] == 200
    token = result["out"]["body"]["entitlement_token"]

    monkeypatch.setenv("RADARX_ENTITLEMENT_JWKS", json.dumps({"keys": [jwk]}))
    monkeypatch.setenv("RADARX_SUPABASE_URL", "http://127.0.0.1:1")
    claims = tokens.verify_entitlement_token(token, now=time.time())
    assert claims.sub == "11111111-2222-3333-4444-555555555555"
    assert claims.email == "ada@example.test"
    assert claims.raw["src"] == "pat" and claims.raw["pat_name"] == "from-node"
    assert claims.exp - claims.iat == 3600 and claims.exp <= claims.raw["pat_exp"]
    # and the engine refuses it as a Supabase token
    with pytest.raises(tokens.TokenInvalid):
        tokens.verify_access_token(token)
    # a different key (a second run of the script) does not verify it
    (tmp_path / "other").mkdir()
    _, other = make_key(tmp_path / "other")
    monkeypatch.setenv("RADARX_ENTITLEMENT_JWKS", json.dumps({"keys": [public_jwk(other.stdout)]}))
    with pytest.raises(tokens.TokenInvalid):
        tokens.verify_entitlement_token(token, now=time.time())


def test_the_env_file_holds_both_secrets_privately_and_the_command_line_holds_none(tmp_path):
    out, proc = make_key(tmp_path)
    env_file = out.with_name(out.name + ".env")
    assert oct(env_file.stat().st_mode & 0o777) == "0o600"
    text = env_file.read_text()
    assert text.startswith('ENTITLEMENT_PRIVATE_KEY="-----BEGIN PRIVATE KEY-----\\n')
    assert "ENTITLEMENT_PUBLIC_JWK='" in text
    public = json.loads(text.split("ENTITLEMENT_PUBLIC_JWK='", 1)[1].split("'", 1)[0])
    assert public["kid"] == public_jwk(proc.stdout)["kid"] and "d" not in public
    assert "supabase secrets set --env-file" in proc.stdout
    assert 'ENTITLEMENT_PRIVATE_KEY="$(cat' not in proc.stdout  # never the key on argv
    assert "BEGIN PRIVATE KEY" not in proc.stdout + proc.stderr


def test_the_key_script_gives_friendly_errors_not_tracebacks(tmp_path):
    env = {"PATH": "/usr/bin:/bin"}
    missing = run_python([str(SCRIPT), "--out", str(tmp_path / "nope" / "key.pem")], env=env)
    assert missing.returncode == 2 and "does not exist" in missing.stderr
    assert "Traceback" not in missing.stderr
    dangling = tmp_path / "dangling.pem"
    dangling.symlink_to(tmp_path / "nowhere")
    done = run_python([str(SCRIPT), "--out", str(dangling)], env=env)
    assert done.returncode == 2 and "exists already" in done.stderr
    assert "Traceback" not in done.stderr
    assert not (tmp_path / "nowhere").exists(), "the script followed the link"
    linked = tmp_path / "linked.pem"
    (tmp_path / "target.pem").write_text("x")
    linked.symlink_to(tmp_path / "target.pem")
    done = run_python([str(SCRIPT), "--out", str(linked)], env=env)
    assert done.returncode == 2 and (tmp_path / "target.pem").read_text() == "x"
