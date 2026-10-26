"""Shared chart-capture helpers for non-Daytona sandbox runtimes.

Providers that execute Python via subprocess (MemoryRuntime, DockerRuntime)
wrap user code with ``build_code_wrapper()`` to intercept ``plt.show()`` and
emit base64 chart artifacts on stdout.  ``extract_artifacts()`` parses the
markers back out so they can be returned as ``Artifact`` objects.
"""

from __future__ import annotations

from ptc_agent.core.sandbox.runtime import Artifact


def build_code_wrapper(code: str) -> str:
    """Wrap user code to capture matplotlib charts as artifacts.

    Matches the Daytona sandbox behavior where plt.show() is intercepted
    and charts are emitted as base64 artifacts. pyplot is patched as it is
    imported rather than imported up front: loading it costs about half a
    second, which code that draws nothing should not pay.
    """
    return f"""\
import sys
import os

# Non-interactive backend, chosen without importing matplotlib.
os.environ['MPLBACKEND'] = 'Agg'


class _ChartCapture:
    \"\"\"Replace pyplot.show the moment the code imports pyplot.\"\"\"

    def find_spec(self, name, path=None, target=None):
        if name != 'matplotlib.pyplot':
            return None
        sys.meta_path.remove(self)
        import importlib.util
        spec = importlib.util.find_spec(name)
        if spec is None or spec.loader is None:
            return spec
        load = spec.loader.exec_module

        def exec_module(plt):
            load(plt)
            _chart_count = [0]

            def _capture_show(*args, **kwargs):
                import io, base64
                _chart_count[0] += 1
                buf = io.BytesIO()
                plt.savefig(buf, format='png', bbox_inches='tight')
                buf.seek(0)
                data = base64.b64encode(buf.read()).decode()
                print(f"__CHART_ARTIFACT__:image/png:chart_{{_chart_count[0]}}.png:{{data}}")
                plt.close('all')

            plt.show = _capture_show

        spec.loader.exec_module = exec_module
        return spec


sys.meta_path.insert(0, _ChartCapture())

# Execute user code
{code}
"""


def extract_artifacts(stdout: str) -> tuple[list[Artifact], str]:
    """Extract __CHART_ARTIFACT__ markers from stdout.

    Returns (artifacts, cleaned_stdout).
    """
    artifacts: list[Artifact] = []
    clean_lines: list[str] = []

    for line in stdout.split("\n"):
        if line.startswith("__CHART_ARTIFACT__:"):
            parts = line.split(":", 3)
            if len(parts) == 4:
                _, mime_type, name, data = parts
                artifacts.append(Artifact(type=mime_type, data=data, name=name))
        else:
            clean_lines.append(line)

    return artifacts, "\n".join(clean_lines)
