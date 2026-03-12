"""
AIProxyHub 注册模块包。

说明：
- 该文件用于将 `register/` 目录标记为 Python package，便于在：
  1) 源码运行（python launcher.py）
  2) PyInstaller 打包后的单文件/目录版 EXE
  中以 `from register.chatgpt_register import ...` 的方式稳定导入。
"""

