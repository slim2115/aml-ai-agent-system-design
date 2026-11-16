"""Pinned, project-local browser tooling; never installs into the app's .venv."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
from importlib import metadata
import json
import os
from pathlib import Path
import platform
import re
import runpy
import subprocess
import sys
import uuid
import zipfile

ROOT = Path(__file__).resolve().parents[2]
LOCK = ROOT / "qa/requirements-browser.lock"
TOOL = ROOT / "var/qa/tooling/browser-python"
BROWSERS = ROOT / "var/qa/tooling/browsers"
WHEELS = ROOT / "var/qa/tooling/browser-wheels"


def _checked(path: Path) -> Path:
    path = path.absolute()
    path.relative_to(ROOT)
    for parent in (path, *path.parents):
        if parent == ROOT.parent:
            break
        if parent.is_symlink() or (hasattr(parent, "is_junction") and parent.is_junction()):
            raise ValueError(f"Browser tooling path must not use links: {parent}")
    if path.resolve() != path:
        raise ValueError("Browser tooling path escapes its fixed project location")
    return path


def _pins() -> dict[str, str]:
    result = {}
    for line in LOCK.read_text(encoding="utf-8-sig").splitlines():
        if not line or line.startswith("#"):
            continue
        match = re.fullmatch(r"([a-z0-9-]+)==([0-9.]+) --hash=sha256:([a-f0-9]{64})", line)
        if not match or match[1] in result:
            raise ValueError("Browser requirements must have unique exact versions and SHA256 hashes")
        result[match[1]] = match[2]
    if result.get("pytest") != "9.1.1" or "playwright" not in result or "pytest-playwright" not in result:
        raise ValueError("Incomplete browser tooling lock")
    return result


def _versions(path: Path) -> dict[str, str]:
    return {dist.metadata["Name"].lower().replace("_", "-"): dist.version
            for dist in metadata.distributions(path=[str(path)])}


def activate() -> dict[str, str]:
    """Call before importing pytest/playwright; safe under Python -I, without PYTHONPATH."""
    _checked(TOOL)
    _checked(BROWSERS)
    expected = _pins()
    if _versions(TOOL) != expected:
        raise ValueError("Pinned browser tools are absent or mismatched; run scripts/qa/Install-BrowserTools.ps1")
    for name in ("pytest", "_pytest", "playwright", "pytest_playwright"):
        module = sys.modules.get(name)
        if module and getattr(module, "__file__", None) and not Path(module.__file__).resolve().is_relative_to(TOOL):
            raise ValueError(f"{name} was imported before activating the isolated browser tools")
    if str(TOOL) not in sys.path:
        sys.path.insert(0, str(TOOL))
    os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(BROWSERS)
    os.environ["PLAYWRIGHT_SKIP_BROWSER_GC"] = "1"
    os.environ["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
    os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
    sys.dont_write_bytecode = True
    return expected


def _child_env() -> dict[str, str]:
    env = dict(os.environ)
    for name in ("PYTHONPATH", "PYTHONHOME", "PYTEST_ADDOPTS", "PYTEST_PLUGINS"):
        env.pop(name, None)
    temp = _checked(ROOT / "var/qa/tooling/tmp")
    temp.mkdir(parents=True, exist_ok=True)
    env.update(TEMP=str(temp), TMP=str(temp), PYTHONDONTWRITEBYTECODE="1",
               PYTHONNOUSERSITE="1", PLAYWRIGHT_BROWSERS_PATH=str(BROWSERS),
               PLAYWRIGHT_SKIP_BROWSER_GC="1", PYTEST_DISABLE_PLUGIN_AUTOLOAD="1")
    return env


def _run(args: list[str]) -> None:
    subprocess.run([sys.executable, "-I", "-B", *args], check=True, cwd=ROOT, env=_child_env())


def _base_versions() -> dict[str, str]:
    return _versions(ROOT / ".venv/Lib/site-packages")


def _write_new(path: Path, value: dict) -> None:
    _checked(path)
    with path.open("x", encoding="utf-8") as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2)
        handle.write("\n")


def install(output: Path) -> dict:
    if sys.version_info[:2] != (3, 12) or sys.platform != "win32" or platform.machine().lower() not in ("amd64", "x86_64"):
        raise ValueError("This wheel lock targets Windows x64 CPython 3.12")
    if not (ROOT / ".rebuild/origin.json").is_file():
        raise ValueError("Missing rebuild provenance")
    for path in (TOOL, BROWSERS, WHEELS, LOCK, output):
        _checked(path)
    allowed = (ROOT / "var/qa", ROOT / "verification/qa-step-04-2026-10-14")
    if not any(output.is_relative_to(parent) for parent in allowed):
        raise ValueError("Use a new output directory under var/qa or step-04 verification")
    output.mkdir(parents=True, exist_ok=False)
    before = _base_versions()
    expected = _pins()
    if before.get("pytest") != expected["pytest"]:
        raise ValueError("Application pytest changed; review the separate browser lock before installing")
    record = {"schema_version": "1.0", "started_at_utc": datetime.now(timezone.utc).isoformat(),
              "status": "ERROR", "packages": expected, "application_environment_modified": None,
              "lock_sha256": hashlib.sha256(LOCK.read_bytes()).hexdigest(),
              "python": platform.python_version(), "platform": platform.platform(),
              "tool_path": str(TOOL.relative_to(ROOT)), "browser_path": str(BROWSERS.relative_to(ROOT)),
              "sources": ["https://playwright.dev/python/docs/intro", "https://pypi.org/project/playwright/",
                          "https://pypi.org/project/pytest-playwright/"]}
    try:
        if TOOL.exists():
            if _versions(TOOL) != expected:
                raise ValueError("Existing tool target does not match lock; preserve it for inspection before reinstalling")
        else:
            WHEELS.mkdir(parents=True, exist_ok=True)
            common = ["-m", "pip", "--isolated", "--disable-pip-version-check", "--no-cache-dir"]
            pinned = ["--require-hashes", "--only-binary=:all:", "--no-deps", "-r", str(LOCK)]
            _run(common + ["download", "--index-url", "https://pypi.org/simple", "--dest", str(WHEELS), *pinned])
            staging = _checked(TOOL.with_name("browser-python-staging-" + uuid.uuid4().hex))
            _run(common + ["install", "--no-index", "--find-links", str(WHEELS), "--target", str(staging),
                           "--no-compile", "--no-warn-script-location", *pinned])
            if _versions(staging) != expected:
                raise ValueError("Installed tools do not match the lock")
            staging.rename(TOOL)
        _run([str(Path(__file__)), "_install-chromium"])
        _run([str(Path(__file__)), "verify", "--output", str(output / "browser-smoke.json")])
        record["status"] = "PASS"
    except Exception as exc:
        record["error_type"] = type(exc).__name__
        raise
    finally:
        after = _base_versions()
        record["application_environment_modified"] = before != after
        record["application_pytest_before"] = before.get("pytest")
        record["application_pytest_after"] = after.get("pytest")
        record["finished_at_utc"] = datetime.now(timezone.utc).isoformat()
        if before != after:
            record["status"] = "FAIL"
        _write_new(output / "installation.json", record)
    if record["status"] != "PASS":
        raise ValueError("Application package versions changed during browser installation")
    return record


def verify(output: Path) -> dict:
    packages = activate()
    from playwright.sync_api import sync_playwright
    import pytest
    import pytest_playwright.pytest_playwright as plugin

    with sync_playwright() as driver:
        executable = Path(driver.chromium.executable_path)
        if not executable.resolve().is_relative_to(BROWSERS):
            raise ValueError("Chromium executable is outside the isolated QA cache")
        browser = driver.chromium.launch(headless=True)
        try:
            page = browser.new_page()
            page.route("**/*", lambda route: route.abort())
            page.set_content("<title>Local QA browser smoke</title><button>Ready</button>")
            assert page.title() == "Local QA browser smoke"
            assert page.get_by_role("button", name="Ready", exact=True).count() == 1
            chromium_version = browser.version
        finally:
            browser.close()
    result = {"schema_version": "1.0", "checked_at_utc": datetime.now(timezone.utc).isoformat(),
              "status": "PASS", "packages": packages, "chromium_version": chromium_version,
              "chromium_executable": str(executable.relative_to(ROOT)),
              "pytest_import": str(Path(pytest.__file__).relative_to(ROOT)),
              "plugin_import": str(Path(plugin.__file__).relative_to(ROOT)),
              "browser_metadata_sha256": hashlib.sha256((TOOL / "playwright/driver/package/browsers.json").read_bytes()).hexdigest(),
              "scope": "Headless Chromium launch and local HTML only; no application, models or network pages"}
    _write_new(output, result)
    return result


def trace_arguments(path: Path) -> list[str]:
    """Open only an existing local QA archive, never a remote URL or arbitrary file."""
    path = _checked(path)
    if not any(path.is_relative_to(root) for root in (ROOT / "var/qa", ROOT / "verification")):
        raise ValueError("Use a local QA trace under var/qa or verification")
    if not path.is_file() or path.suffix.lower() != ".zip" or not zipfile.is_zipfile(path):
        raise ValueError("Trace must be an existing ZIP archive")
    with zipfile.ZipFile(path) as archive:
        if not any(name.endswith(".trace") for name in archive.namelist()):
            raise ValueError("The ZIP archive does not contain a Playwright trace")
    return ["playwright", "show-trace", str(path), "--host", "127.0.0.1"]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["install", "verify", "trace", "_install-chromium"])
    parser.add_argument("--output-dir")
    parser.add_argument("--output")
    parser.add_argument("--path", help="Existing local trace.zip, for trace only")
    args = parser.parse_args()
    if args.command == "_install-chromium":
        activate()
        sys.argv = ["playwright", "install", "chromium"]
        runpy.run_module("playwright", run_name="__main__")
    elif args.command == "install":
        path = args.output_dir or f"var/qa/tooling/install-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')}-{uuid.uuid4().hex[:8]}"
        result = install(_checked(ROOT / path))
        print(json.dumps({"status": result["status"], "output_dir": path}))
    elif args.command == "trace":
        if not args.path:
            parser.error("trace requires --path")
        arguments = trace_arguments(ROOT / args.path)
        activate()
        sys.argv = arguments
        runpy.run_module("playwright", run_name="__main__")
    else:
        if not args.output:
            parser.error("verify requires --output (a new JSON file)")
        print(json.dumps(verify(_checked(ROOT / args.output))))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
