CONFIG = {
    "random_seed": 7,

    # 最大迭代次数
    "max_iterations": 30,

    # 连续多少轮没有改善就停止
    "patience": 8,

    # 连续多少轮没有改善就切换到全局探索
    "explore_after_stagnation": 3,

    # 局部搜索初始扰动范围
    "initial_radius": 0.20,

    # 局部搜索最小扰动范围
    "min_radius": 0.03,

    # 参数范围：目前仍然是虚拟变量
    "parameter_bounds": {
        "x1": [0.0, 1.0],
        "x2": [0.0, 1.0],
        "x3": [0.0, 1.0],
    },

    # 大语言模型配置
    "llm": {
        "provider": "siliconflow",
        "model": "Qwen/Qwen3-8B",
        "base_url": "https://api.siliconflow.cn/v1",
        "temperature": 0.2,
        "max_tokens": 1000,
        "timeout": 60.0,
        "enable_thinking": False,
    },

    # 文献问答 Agent 配置（与原有优化 Agent 分开）
    "qa": {
        "model": "Qwen/Qwen3-8B",
        "base_url": "https://api.siliconflow.cn/v1",
        "timeout": 90.0,
        "max_retries": 1,
        # Qwen3 关闭思考模式，保证输出 JSON 和回答的速度
        "enable_thinking": False,
        # 关键词理解
        "keyword_temperature": 0.1,
        "keyword_max_tokens": 300,
        "max_keywords": 6,
        # 回答生成
        "answer_temperature": 0.2,
        "answer_max_tokens": 1500,
        # 检索
        "per_keyword_limit": 8,
        "max_evidence": 8,
        "max_per_paper": 2,
    },
}