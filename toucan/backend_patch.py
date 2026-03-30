"""
backend.py 集成指南 — 只需 3 处修改

修改1 (import): 在文件顶部添加
    try:
        from toucan.toucan_api import register_toucan_routes
        TOUCAN_AVAILABLE = True
    except ImportError:
        TOUCAN_AVAILABLE = False

修改2 (注册路由): 在 app.add_middleware(...) 之后添加
    if TOUCAN_AVAILABLE:
        register_toucan_routes(app)

修改3 (环境变量):
    export DEEPSEEK_API_KEY="xxx"
    export SMITHERY_API_KEY="xxx"   # 可选
"""
if __name__ == "__main__":
    print(__doc__)
