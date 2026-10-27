"""Small entry path for resident HTTP services, separate from maintenance CLI."""

import sys


def main(command, *, stderr=None):
    stderr = sys.stderr if stderr is None else stderr
    if command not in {"web-serve", "metadata-serve"}:
        stderr.write("操作失败（错误代码：invalid_command）\n")
        return 2
    try:
        from clash_sub.config import load_config
        from clash_sub.paths import repo_root, config_path
        from clash_sub.metadata_server import listener_from_environment
        root = repo_root()
        config = load_config(config_path(root), root)
        listener = listener_from_environment()
        if command == "web-serve":
            from clash_sub.web_server import serve
            serve(config, listener)
        else:
            from clash_sub.metadata import TrafficMetadataStore
            from clash_sub.metadata_server import serve
            serve(TrafficMetadataStore(config), listener)
        return 0
    except Exception:
        code = "web_serve_failed" if command == "web-serve" else "metadata_serve_failed"
        stderr.write("操作失败（错误代码：%s）\n" % code)
        return 1
