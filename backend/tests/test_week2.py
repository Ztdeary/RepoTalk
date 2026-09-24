from app.rag.repo_loader import clone_repo,iter_code_file # 导入克隆仓库和遍历代码文件函数
from app.rag.code_splitter import split_code_file # 导入代码分割器函数
import ast # 导入Python的抽象语法树模块
import random # 导入随机数模块
import textwrap # 导入文本包装模块
from pathlib import Path # 导入路径模块

# Week2 验收脚本
def main():
  CACHE=Path(r"D:\RepoTalk开源仓库代码问答Agent项目\repotalk\backend\tests\.cache\requests") # 缓存目录
  root=CACHE if CACHE.exists() else clone_repo("https://github.com/psf/requests") # 如果缓存目录存在,则直接使用,否则克隆仓库
  # 本地缓存,避免每次联网克隆
  chunks,py_files=[],0 # 初始化chunk列表和py_files计数器
  for p in iter_code_file(root): # 遍历所有代码文件
    if p.suffix==".py": # 如果是Python文件
      py_files+=1 # 增加py_files计数器
    chunks+=split_code_file(p,"requests",root) # 分割文件内容,并添加到chunk列表
  print(f"共切出{len(chunks)}块,其中Python文件{py_files}个") # 打印切出的块数和Python文件数

  # 判断"file_path"和"start_line"元数据键是否存在
  for c in chunks:
    assert "file_path" in c["metadata"] and "start_line" in c["metadata"]
    # 断言(assert)"file_path"和"start_line"元数据键存在
    # and是逻辑与操作,只有当两个条件都为True时,断言才通过

  # 全量体检：遍历所有Python块，统计+列出异常
  checked,bad=0,[] # 初始化checked计数器和bad列表
  for c in chunks:
    if c["metadata"]["language"]!="python": # 如果不是Python文件
      continue 
    checked+=1 # 增加checked计数器
    try:
      ast.parse(c["content"]) # ast.parse()函数尝试解析Python代码
    except SyntaxError as e: # SyntaxError异常表示Python代码语法错误
      try:
        ast.parse(textwrap.dedent(c["content"])) # 去掉整体缩进
      except SyntaxError as e: # SyntaxError异常表示Python代码语法错误
        bad.append((c["metadata"]["file_path"],c["metadata"]["start_line"],str(e))) # 添加到bad列表
  print(f"体检完成:检查{checked}块Python,异常{len(bad)}块") # 打印当前检查的块数和异常块数
  for f,l,e in bad: # 遍历异常
    print(f"⚠ 疑似被腰斩:{f},{l},{e}") # 打印异常信息

  # 随机抽取三个chunks验收
  py_chunks=[ c for c in chunks if c["metadata"]["language"]=="python"] # 筛选出Python块
  for c in random.sample(py_chunks,3): # 随机抽取三个Python块 
    m=c["metadata"] 
    # 获取元数据
    # 打印文件路径、起始行、结束行、符号、内容前300个字符
    print ( f"\n---文件路径:{m['file_path']}---起始行号{m['start_line']}---结束行号{m['end_line']}---symbol={m['symbol']!r}") 
    # !r是string版的`repr`,空串会显示成`''`
    print (c[ "content" ][: 300 ])
  
main()
    

    

