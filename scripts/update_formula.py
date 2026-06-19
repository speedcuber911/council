#!/usr/bin/env python3
"""Regenerate Formula/council.rb for a release.

Run after tagging a new version and publishing the GitHub release:

    python scripts/update_formula.py 0.3.0

It:
  1. downloads the GitHub release tarball and computes its sha256,
  2. resolves the runtime deps in a clean venv and pulls each sdist's
     URL + sha256 from PyPI,
  3. writes Formula/council.rb.

Requires network. No third-party deps.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import tempfile
import urllib.request
from pathlib import Path

REPO = "speedcuber911/council"
ROOT = Path(__file__).resolve().parent.parent
FORMULA = ROOT / "Formula" / "council.rb"

# Packages that are not runtime deps of council (build/dev only).
EXCLUDE = {"pip", "setuptools", "wheel", "llm-council-cli"}


def sh(cmd: list[str]) -> str:
    return subprocess.check_output(cmd, text=True)


def tarball_sha256(version: str) -> str:
    url = f"https://github.com/{REPO}/archive/refs/tags/v{version}.tar.gz"
    print(f"  fetching {url}")
    data = urllib.request.urlopen(url, timeout=60).read()
    return hashlib.sha256(data).hexdigest()


def resolve_resources(version: str) -> list[tuple[str, str, str]]:
    """Install council in a temp venv, return [(name, sdist_url, sha256), ...]."""
    with tempfile.TemporaryDirectory() as tmp:
        venv = Path(tmp) / "venv"
        sh([sys.executable, "-m", "venv", str(venv)])
        pip = venv / "bin" / "pip"
        sh([str(pip), "install", "-q", f"git+https://github.com/{REPO}@v{version}"])
        frozen = sh([str(pip), "freeze"]).splitlines()

    resources = []
    for line in frozen:
        if "==" not in line or "@" in line:
            continue
        name, ver = line.split("==")
        if name.lower() in EXCLUDE:
            continue
        meta = json.load(urllib.request.urlopen(
            f"https://pypi.org/pypi/{name}/{ver}/json", timeout=30))
        sdist = next((f for f in meta["urls"] if f["packagetype"] == "sdist"), None)
        if not sdist:
            raise SystemExit(f"no sdist for {name} {ver}")
        resources.append((name, sdist["url"], sdist["digests"]["sha256"]))
        print(f"  resource {name} {ver}")
    return sorted(resources)


def render(version: str, tarball_hash: str, resources: list[tuple[str, str, str]]) -> str:
    blocks = "\n\n".join(
        f'  resource "{n}" do\n    url "{u}"\n    sha256 "{h}"\n  end'
        for n, u, h in resources
    )
    return f'''class Council < Formula
  include Language::Python::Virtualenv

  desc "LLM council in your terminal — Claude Code, Codex & Ollama cloud deliberate"
  homepage "https://github.com/{REPO}"
  url "https://github.com/{REPO}/archive/refs/tags/v{version}.tar.gz"
  sha256 "{tarball_hash}"
  license "MIT"

  depends_on "python@3.12"

{blocks}

  def install
    virtualenv_install_with_resources
  end

  test do
    assert_match "council", shell_output("#{{bin}}/council version")
  end
end
'''


def main() -> None:
    if len(sys.argv) != 2:
        sys.exit("usage: python scripts/update_formula.py <version>")
    version = sys.argv[1].lstrip("v")
    print(f"Updating formula for v{version}")
    th = tarball_sha256(version)
    resources = resolve_resources(version)
    FORMULA.parent.mkdir(parents=True, exist_ok=True)
    FORMULA.write_text(render(version, th, resources))
    print(f"Wrote {FORMULA} ({len(resources)} resources)")


if __name__ == "__main__":
    main()
