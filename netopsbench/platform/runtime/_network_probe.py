"""Read-only routing checks inside one Docker exec; no NetOpsBench imports."""

import json
import subprocess


def probe(run=subprocess.run):
    checks = {}
    for name, command in (
        ("bgp", ["vtysh", "-c", "show ip bgp summary"]),
        ("interfaces", ["bash", "-lc", "show interfaces status"]),
    ):
        try:
            result = run(command, capture_output=True, text=True, check=False, timeout=60)
            checks[name] = {"returncode": result.returncode, "stdout": result.stdout or ""}
        except (subprocess.TimeoutExpired, OSError) as error:
            checks[name] = {"returncode": -1, "stdout": "", "error": type(error).__name__}
    return {"version": 1, "checks": checks}


if __name__ == "__main__":
    print(json.dumps(probe()))
