"""AIPhotoArrange 桌面 GUI 客户端。

模块组成：
- config_io.py      pipeline_config.yaml 的读写（保留注释）
- log_widget.py     实时日志文本框 + sys.stdout/stderr 重定向
- pipeline_runner.py 后台线程执行 run_pipeline.py，实时转发日志
- power.py         流水线运行期间阻止 Windows 睡眠（SetThreadExecutionState）
- app.py            主窗口，组装配置区/运行控制/日志区

所有模块相互独立、无对流水线脚本的导入依赖，便于后续 Nuitka 编译。
"""