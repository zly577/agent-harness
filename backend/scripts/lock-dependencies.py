"""Lock Python 3.12 Windows/Linux dependencies against the tested environment.

Run from a validated Python 3.12 environment with uv installed. This never
installs or upgrades packages; review and test the resulting lock before release.
"""
import argparse
import importlib.metadata as metadata
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile


def canonical(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def pins(path: Path) -> dict[str, str]:
    result = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        match = re.fullmatch(r"([\w.-]+)==([^\s;]+)", line)
        if not match:
            raise ValueError(f"Expected a fixed platform pin, got: {line}")
        result[canonical(match[1])] = match[2]
    return result


def main() -> None:
    backend = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=backend / "requirements-lock.txt")
    args = parser.parse_args()
    if sys.version_info[:2] != (3, 12):
        parser.error("Use the validated Python 3.12 environment to generate this lock")
    uv = shutil.which("uv")
    if not uv:
        parser.error("uv is required; install it separately before generating the lock")

    # metadata.version follows the active interpreter's distribution precedence.
    # Enumerating dist.version directly can pick stale duplicate dist-info files.
    names = {canonical(dist.metadata["Name"]) for dist in metadata.distributions() if dist.metadata["Name"]}
    tested = [f"{name}=={metadata.version(name)}" for name in sorted(names)]
    resolved = {}
    with tempfile.TemporaryDirectory(prefix="harness-dependency-lock-") as temporary:
        folder = Path(temporary)
        constraints = folder / "tested.txt"
        constraints.write_text("\n".join(tested) + "\n", encoding="utf-8")
        for platform in ("windows", "linux"):
            output = folder / f"{platform}.txt"
            subprocess.run([
                uv, "pip", "compile", str(backend / "requirements.txt"),
                "--python-version", "3.12", "--python-platform", platform,
                "--constraint", str(constraints), "--output-file", str(output),
                "--no-header", "--no-annotate", "--quiet",
            ], check=True)
            resolved[platform] = pins(output)

    windows, linux = resolved["windows"], resolved["linux"]
    lines = [
        "# Resolved dependency versions for Python 3.12 on Windows and Linux.",
        "# Source: requirements.txt; update procedure: docs/stability-improvements.md.",
    ]
    for name in sorted(windows.keys() | linux.keys()):
        if name in windows and windows.get(name) == linux.get(name):
            lines.append(f"{name}=={windows[name]}")
        else:
            for packages, marker in ((windows, "win32"), (linux, "linux")):
                if name in packages:
                    lines.append(f'{name}=={packages[name]} ; sys_platform == "{marker}"')
    args.output.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"Wrote {len(lines) - 2} pins to {args.output}")


if __name__ == "__main__":
    main()
