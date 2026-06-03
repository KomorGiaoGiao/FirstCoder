"""FirstCoder Textual/TUI 入口模块。"""

from firstcoder.app.factory import create_firstcoder_app
from firstcoder.app.tui import FirstCoderApp, FirstCoderTuiConfig

__all__ = ["FirstCoderApp", "FirstCoderTuiConfig", "create_firstcoder_app"]
