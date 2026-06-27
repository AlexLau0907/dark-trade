"""
一键启动脚本
用法: python run.py
"""
import sys
import os

# 确保项目根目录在 sys.path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

if __name__ == "__main__":
    import uvicorn
    from server.config import HOST, PORT

    print("启动轻量级财经数据 API 服务器...")
    print(f"RawData 目录: {os.path.join(os.path.dirname(__file__), 'RawData')}")

    uvicorn.run(
        "server.main:app",
        host=HOST,
        port=PORT,
        reload=False,
        log_level="info",
    )
