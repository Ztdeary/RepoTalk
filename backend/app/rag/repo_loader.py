from pathlib import Path # pathlib:面向对象的路径操作库,比os.path更现代、更可读
import os # os.environ:读环境变量,用于拼装git子进程的环境
import tempfile # tempfile.mkdtemp:创建唯一临时目录,避免克隆冲突
import subprocess # subprocess.run:执行外部命令(git clone),同步阻塞调用
from urllib.parse import urlparse # urlparse:解析URL,把"https://github.com/psf/requests.git"拆成协议/域名/路径等部分

# 文件后缀白名单:只有这些后缀的文件才会被喂进tree-sitter解析器
# 别把图片/二进制/配置文件也喂进去——tree-sitter对非代码文件会直接报错或返回空
# ".md"暂不处理,Week 3再加上(那时加RAG增强时可能需要README内容)
CODE_EXTENSIONS = {".py", ".js", ".ts", ".tsx", ".java", ".go", ".rs"}


def clone_repo(repo_url: str) -> Path:
    """浅克隆远程仓库到本地临时目录。
    
    克隆策略:依次尝试3个URL源,直连成功就返回;失败自动降级到镜像。
    为什么要降级?GitHub在国内访问不稳定,直连经常超时,镜像站(gitee/gitclone)兜底。
    
    克隆方式:--depth 1(只拉最后一个提交的快照)
    为什么不用完整克隆?RAG只关心当前代码,不需要历史commit记录,浅克隆快10倍以上。
    
    返回:Path对象——克隆出来的临时目录路径,调用方负责用完删掉。
    """
    # 第一步:清洗URL——用户复制粘贴时常带入空格、反引号等
    # strip():去首尾空白。strip("`"):再去首尾反引号(Markdown复制后遗症)。再strip()兜底。
    repo_url = repo_url.strip().strip("`").strip()
    
    # 创建唯一临时目录:repotalk_开头,mkdtemp自动加随机后缀保证不重名
    # Path()把字符串路径转成pathlib.Path对象,后续用str(tmp)传给subprocess
    tmp = Path(tempfile.mkdtemp(prefix="repotalk_"))
    
    # 从原始URL里解析owner/repo部分——gitclone镜像需要这个格式
    # urlparse("https://github.com/psf/requests.git").path -> "/psf/requests.git"
    # [1:]去掉开头的"/",.removesuffix(".git")去掉末尾的.git后缀
    owner_repo = urlparse(repo_url).path[1:].removesuffix(".git")
    # 举例:repo_url="https://github.com/psf/requests.git"
    # owner_repo最终="psf/requests"
    
    # 克隆候选URL列表:依次尝试,成功一个就停
    # 策略:直连(最快) → gitee镜像(国内最快github镜像) → gitclone镜像(备选)
    CANDIDATES = [
        repo_url,  # 候选1:原始URL,直连GitHub
        repo_url.replace(  # 候选2:gitee镜像
            "https://github.com/",   # 源前缀
            "https://gitee.com/mirrors/"  # 目标前缀——把github.com替换成gitee.com/mirrors
        ).rstrip("/") + "/",  # 去掉末尾多余的"/"后再加一个,保证URL格式规范
        "https://gitclone.com/github.com/" + owner_repo,  # 候选3:gitclone镜像,拼接刚才解析出的owner/repo
    ]
    
    last_err = None # 记录最后一次失败的异常——三个全挂时抛给调用方,让日志里能看到原因
    
    # 禁用git的交互式凭据提示:
    # 如果仓库地址不存在或是私有仓库,git默认会弹出"用户名/密码?"提示框
    # 但worker是后台进程,没人应答会一直挂着直到job_timeout(600秒)才被杀
    # GIT_TERMINAL_PROMPT=0:告诉git"别问了,直接报错"
    # GCM_INTERACTIVE=never:Git Credential Manager也别弹提示
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GCM_INTERACTIVE": "never"}
    
    # 依次尝试每个候选URL
    for url in CANDIDATES:
        try:
            subprocess.run(
                ["git", "clone", "--depth", "1", url, str(tmp)],
                # ["git", "clone", "--depth", "1", url, str(tmp)]:
                # 这是shell命令的参数列表形式(不用字符串),避免shell注入风险
                # --depth 1:浅克隆,只拉最后一个commit,不存历史
                # url:要克隆的仓库地址
                # str(tmp):目标目录(subprocess要str,tmp是Path对象,所以转一下)
                check=True,
                # check=True:返回码非0时抛CalledProcessError(否则subprocess.run静默返回,你不知道成功没)
                timeout=300,
                # timeout=300秒(5分钟):防止git卡死(比如网络彻底不通),超时抛TimeoutExpired
                capture_output=True,
                # capture_output=True:把stdout/stderr捕获到内存里,失败时能在e.stderr里看到错误信息
                text=True,
                # text=True:捕获的输出按UTF-8解码成str(默认是bytes),方便打印日志
                env=env,
                # env=env:把上面拼的环境变量传进去,禁用交互式提示
            )
            # 成功:如果走了镜像(不是原始URL),打个日志让运维知道"直连挂了,用的镜像"
            if url != repo_url:
                print(f"[clone] 直连失败,已走镜像: {url}")
            return tmp  # 立刻返回,不用再试后面的候选
        except subprocess.CalledProcessError as e:
            # CalledProcessError:git返回码非0(比如仓库不存在、网络超时、权限拒绝)
            print(f"[clone] {url} 失败: {e.stderr}")  # e.stderr就是git的错误输出,原样打印
            last_err = e  # 记下来继续试下一个候选
    
    # 三个URL全挂了——抛最后一次的异常,让调用方(parse_repo_task)能catch到并更新RepoStatus.FAILED
    raise last_err


def iter_code_file(root: Path):
    """遍历仓库目录,产出所有符合条件的代码文件路径。
    
    过滤规则(按顺序应用,任一不满足就跳过):
    1. 只保留文件,跳过目录(.git是目录,先被这关过滤了,但显式写一遍更清晰)
    2. 跳过依赖目录(node_modules/.venv/__pycache__等)——里面不是项目源码,且量巨大
    3. 文件后缀必须在 CODE_EXTENSIONS 白名单里(.py/.js/.ts等)
    4. 空文件(0字节)和超大文件(>200KB)跳过——空文件没意义,超大文件是lock文件或二进制伪装
    
    为什么用yield而不是return list?
    yield是生成器,迭代时才一个个产出Path对象,内存占用恒定(同时只有1个Path在内存里)。
    如果return list,node_modules被排除了还好,但大型仓库也可能有上千个文件,list一次性全塞进内存没必要。
    """
    IGNORE_DIRS = {
        "node_modules",   # 前端依赖目录,量巨大且不是源码
        ".git",            # Git版本控制目录,必须跳过
        "dist", "build",   # 构建产物目录,不是源码
        "venv", ".venv",   # Python虚拟环境
        "__pycache__",     # Python字节码缓存
    }
    
    # root.rglob("*"):递归遍历root下所有文件和目录
    # rglob = recursive glob,等价于 root.glob("**/*")
    # 返回一个生成器,每次产出一个Path对象
    for path in root.rglob("*"):
        # 过滤1:只保留文件。path.is_file()返回True才是普通文件,目录/符号链接返回False
        if not path.is_file():
            continue
        
        # 过滤2:跳过依赖目录
        # path.parts:把路径拆成元组,比如PosixPath("/foo/bar/node_modules/x.js").parts -> ('/', 'foo', 'bar', 'node_modules', 'x.js')
        # 用any()逐个检查:路径的任一段落在IGNORE_DIRS里就跳过
        if any(part in IGNORE_DIRS for part in path.parts):
            continue
        
        # 过滤3:后缀白名单
        # path.suffix:文件后缀,比如Path("main.py").suffix -> ".py"。Path("Makefile").suffix -> ""
        # not in CODE_EXTENSIONS:后缀不在白名单里就跳过(比如.md/.json/.yml等)
        if path.suffix not in CODE_EXTENSIONS:
            continue
        
        # 过滤4:文件大小检查
        # path.stat() -> os.stat系统调用,返回文件的元信息对象
        # .st_size:文件字节数。stat()只调一次,存进size变量,下面判断用,避免两次stat
        size = path.stat().st_size
        if size == 0:
            continue  # 空文件:没内容,tree-sitter解析也是空结果,纯粹浪费时间
        if size > 200_000:
            continue  # 超大文件(>200KB):通常是lock文件、二进制伪装、或生成的巨型单文件
            # 200KB阈值怎么来的?正常单文件函数/类不会这么大。超过的基本是机器生成的产物
        
        # 通过所有过滤 -> 产出这个路径
        # yield:生成器产出,函数暂停在这里等下一轮迭代
        yield path
