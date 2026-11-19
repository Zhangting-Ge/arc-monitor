"""用法：
    python -m arcmon run      # 抓取、对比、推送（GitHub Actions 每 10 分钟跑一次）
    python -m arcmon digest   # 发每日汇总
    python -m arcmon test     # 连接测试：确认能抓到数据、Bark 能收到
加 --dry-run 只打印不推送。
"""
from __future__ import annotations

import argparse
import logging
import sys

from .config import ConfigError, load_config
from .fetch import Http
from .monitor import digest, run, selftest
from .notify import Bark
from .state import State


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="arcmon", description="始祖鸟德国正价站/奥莱 上新与库存监控")
    ap.add_argument("mode", choices=["run", "digest", "test"])
    ap.add_argument("--config", default="config.yaml")
    ap.add_argument("--state", default="state")
    ap.add_argument("--dry-run", action="store_true", help="只打印推送内容，不真的发")
    ap.add_argument("-v", "--verbose", action="store_true")
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if a.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    try:
        cfg = load_config(a.config)
    except (ConfigError, OSError) as e:
        print(f"配置错误：{e}", file=sys.stderr)
        return 2

    bark = Bark(dry_run=a.dry_run)
    if a.mode == "test":
        return 0 if selftest(cfg, Http(), bark) else 1
    state = State(a.state)
    if a.mode == "digest":
        digest(cfg, state, bark)
        return 0
    run(cfg, state, Http(), bark)
    return 0


if __name__ == "__main__":
    sys.exit(main())
