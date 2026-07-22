"""不可检视文件过滤规则（纯数据，无逻辑）。

编辑此文件即可调整过滤策略，无需修改代码逻辑。
规则来源：对齐 PR-Agent language_extensions.toml（battle-tested 业界标准）。
"""

# ── 扩展名黑名单 ─────────────────────────────────────────────────────────────
SKIP_EXTENSIONS = frozenset({
    # 图片 / 图标
    ".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp", ".svg", ".ico",
    ".tiff", ".tif", ".raw", ".psd",
    # 音视频
    ".mp3", ".mp4", ".wav", ".ogg", ".webm", ".avi", ".mov", ".mkv", ".flac",
    # 文档 / 表格（二进制格式，非纯文本）
    ".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx",
    # 压缩包 / 归档
    ".zip", ".tar", ".gz", ".tgz", ".bz2", ".xz", ".zst", ".rar", ".7z",
    # 原生二进制 / 编译产物
    ".exe", ".dll", ".so", ".dylib", ".a", ".o", ".obj", ".bin",
    ".pyc", ".pyd", ".pyo", ".class", ".whl", ".jar", ".war", ".egg",
    # 字体
    ".ttf", ".woff", ".woff2", ".eot", ".otf",
    # 锁文件（通用 .lock 扩展名）
    ".lock", ".lockb", ".snap",
    # 日志
    ".log",
    # 数据 / ML 产物
    ".csv", ".tsv", ".dat",
    ".pkl", ".pickle", ".npy", ".npz",
    ".pt", ".pth", ".ckpt", ".safetensors", ".onnx", ".pb", ".tflite",
    ".h5", ".hdf5", ".parquet",
    # 数据库文件
    ".db", ".sqlite", ".sqlite3",
})

# 精确文件名匹配（basename），覆盖无扩展名或特殊命名的锁文件
SKIP_BASENAMES = frozenset({
    "package-lock.json", "yarn.lock", "pnpm-lock.yaml",
    "Pipfile.lock", "poetry.lock",
    "Cargo.lock", "composer.lock", "go.sum",
    "Gemfile.lock", "pubspec.lock",
})

# 路径段黑名单：生成物 / 第三方依赖目录
SKIP_PATH_SEGMENTS = (
    "node_modules/", "dist/", "build/", "__pycache__/",
    ".git/", "vendor/", "third_party/", "site-packages/",
)

# 后缀黑名单：压缩/生成的 JS/CSS，以及 protobuf 生成的 Go 文件
SKIP_NAME_SUFFIXES = (".min.js", ".min.css", ".bundle.js", ".map", ".pb.go")
