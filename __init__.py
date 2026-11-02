"""Hermes 目录插件入口，与 wheel 入口共享注册实现."""

if __package__:
    from .hermes_lark_streaming import register
else:
    from hermes_lark_streaming import register

__all__ = ["register"]
