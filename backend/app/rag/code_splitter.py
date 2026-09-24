import tree_sitter_language_pack as tsl # 导入tree-sitter语言包
from tree_sitter import Parser # 导入tree-sitter解析器
from pathlib import Path # 导入路径模块,比os.path更方便

# 语言映射：文件后缀->tree-sitter 语言名
SUPPORTED={
  "py":"python",
  "js":"javascript",
  "ts":"typescript",
  "tsx":"typescript",
  "java":"java",
  "go":"go",
  "rs":"rust",
}

# 各语言中代表“可独立切分单元”的语法节点类型
# 只有这些节点会被切成块，保证边界不腰斩
SPLITTABLE_NODES={
  "python":{"function_definition","class_definition"},
  "javascript":{"function_declaration","class_declaration","method_definition"},
  "typescript":{"function_declaration","class_declaration","method_definition"},
  "java":{"method_declaration","class_declaration","interface_declaration"},
  "go":{"function_declaration","method_declaration","type_declaration"},
  "rust":{"function_item","impl_item","struct_item","enum_item"},
}

# 创建指定语言的tree-sitter解析器
def get_parser(lang_name:str)->Parser:
  parser=Parser() # 创建解析器实例
  parser.language=tsl.get_language(lang_name) # 设置解析器的语言
  return parser # 返回解析器实例

# 创建tree-sitter切分器
def split_code_file(path:Path,repo_name:str,root:Path,max_chars:int=1200)->list[dict]:
  """手写 tree-sitter 切分：按函数/类边界切，零腰斩
  传入代码文件路径、仓库名称、根目录、最大字符数
  返回切分后的代码块列表
  每个代码块是一个字典，包含代码块的类型、代码块的代码、代码块的起始行号、代码块的结束行号
  """
  # 1.语言检查
  lang_name=SUPPORTED.get(path.suffix.lstrip(".")) 
  # 获取文件后缀对应的tree-sitter语言名
  # 如果语言名不存在，则返回空列表
  if not lang_name:
    return []

  # 2.解析源码
  text=path.read_text(encoding="utf-8",errors="ignore") # 读取文件内容，忽略编码错误
  if not text.strip():
    # 如果文件为空，则跳过。text.strip()若空字符串，返回False 加上not，判断为真
    # 不用if text==""是因为内容是" \n"的文件判不出来（它不是空串，是纯空白）
    # text.strip() — 去掉字符串两端的空白（空格、`\n`、`\t`、`\r`）
    return []

  # 3.解析AST 
  """AST是语法树的缩写,是代码的抽象表示
  它包含了代码的所有语法节点，包括函数、类、方法等
  解析器会根据tree-sitter的语言包,将源码解析成AST
  """
  parser=get_parser(lang_name) #创建对应语言的解析器实例
  tree=parser.parse(text.encode()) 
  # encode()将文本转成字节码后给解析器解析，解析器只能解析字节码，不能解析字符串
  rel_path=path.relative_to(root) # 获取文件相对于根目录的路径
  chunks=[] # 之后用来装载切分后的代码块列表
  splittable_types=SPLITTABLE_NODES.get(lang_name,set())
  # 获取当前语言的可独立切分单元的语法节点类型
  # 如果语言名不存在，则返回空集合

  # 4.递归遍历AST,收集函数/类节点的函数
  def collect(node):
    # 如果目标是函数/类节点
    if node.type in splittable_types:
      # 从AST提取真实名称（非正则猜测）
      name_node=node.child_by_field_name("name") 
      # 通过child_by_field_name(“name”)获取子节点名称
      # 不用正则表达式是因为async def抓不到
      # 不用索引是因为子节点名称是可变的，不能用索引获取
      symbol=name_node.text.decode() if name_node else ""
      # decode()将函数/类名称的字节码转成字符串，赋值给symbol

      # 按行号划分代码块
      start_line=node.start_point[0]+1 # point[0]是当前行号，+1是因为行号从1开始，而不是0开始
      end_line=node.end_point[0]+1 # end_point[0]直接指向最后一行的位置，+1转换为人类习惯的行号

      # 收集代码块信息的字典
      chunk={
        "content":node.text.decode(), # 将文本内容转换为字符串
        "metadata":{ 
          "repo":repo_name, # 仓库名称
          "file_path":str(rel_path), # 文件路径
          "language":lang_name, # 语言名称
          "start_line":start_line, # 起始行号
          "end_line":end_line, # 结束行号
          "symbol":symbol, # 函数/类名称
        }
      }
      
      # 超大函数：不切，直接保留，标注（large）
      if len(chunk["content"])>max_chars: # 如果代码块内容超过最大字符数
        chunk["metadata"]["symbol"]+=" (large)" # 给函数/类名称添加（large）
        # ["metadata"]["symbol"] - 访问字典的属性要用["key"]，访问对象才用obj.key

      chunks.append(chunk) # 将代码块字典添加到列表中
      return # 不再递归子节点（嵌套函数包含在外层中）

    # 不是目标节点，则递归检查子节点
    for child in node.children:
      collect(child)

  collect(tree.root_node) # 正式开始递归遍历AST，收集函数/类节点

  # 5.提取模块级代码（import、全局变量等）
  # 遍历顶层节点，把连续的"非函数/类"节点合并成块
  # 只遍历顶层节点:
  # 第 4 步"吃掉了"函数/类及其内部一切，
  # 第 5 步只负责收拾 顶层剩余 的边角料。
  # 如果第 5 步也深入内部，就会从函数里抢代码，造成重复和混乱。
  module_nodes=[] # 用来装载模块级代码的节点列表
  for child in tree.root_node.children: # 遍历顶层节点的子节点
    if child.type not in splittable_types: # 如果子节点不是函数/类节点
      module_nodes.append(child) # 则把它加入模块级代码节点列表
    else: # 如果还有剩余的函数/类节点
      if module_nodes: # 先判断如果模块级代码节点列表不为空
        chunks.append(merge_module_nodes(module_nodes, rel_path, repo_name, lang_name))
        # merge_module_nodes()为后面定义的函数，用于合并模块级代码节点列表，返回一个代码块字典
        module_nodes=[] # 清空模块级代码节点列表,准备下一次合并
  if module_nodes: # 最后判断如果模块级代码节点列表还不为空
    chunks.append(merge_module_nodes(module_nodes,rel_path,repo_name,lang_name)) 
    # 合并剩余的模块级代码节点列表

  # 6.按起始行号排序
  chunks.sort(key=lambda x: x["metadata"]["start_line"])

  # 7.精确去重:同一文件同一行区间的块只保留第一条
  # 防重复入库(如任务重跑、并发写入),不影响overload桩(它们行号不同)
  seen=set() # 记录已出现的(文件,起行,止行)
  unique=[]
  for c in chunks:
    key=(c["metadata"]["file_path"],c["metadata"]["start_line"],c["metadata"]["end_line"])
    if key in seen:
      continue
    seen.add(key)
    unique.append(c)
  chunks=unique

  return chunks # 返回去重后的代码块列表
  
# 定义合并模块级代码节点列表的函数
def merge_module_nodes(nodes:list,rel_path:Path,repo_name:str,lang_name:str)->dict:
  """把连续的模块级AST节点合并成一个块"""
  start_line=nodes[0].start_point[0]+1 # 从第一个节点的起始行号开始
  end_line=nodes[-1].end_point[0]+1 # 从最后一个节点的结束行号开始
  content="\n".join(n.text.decode() for n in nodes)
  # 遍历节点给n,将n的文本内容转换为字符串，通过join()方法用\n拼接起来

  return {
    "content":content,
    "metadata":{
      "repo":repo_name, # 仓库名称
      "file_path":str(rel_path), # 文件路径
      "language":lang_name, # 语言名称
      "start_line":start_line, # 起始行号
      "end_line":end_line, # 结束行号
      "symbol":"", # 模块级代码没有symbol
    }
  }
