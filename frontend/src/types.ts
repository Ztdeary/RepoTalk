/* 集中定义前后端共用的TypeScript类型(Repo、Source、ChatMessage、RepoStatus),让所有组件共用同一套数据结构。*/

// 仓库状态类型:与后端RepoStatus字符串常量完全一致 
// 用"字符串字面量联合类型"而不是枚举(enum) 
// 原因:前后端直接传字符串,不需要额外的序列化/反序列化 
// 'pending' | 'cloning' | ... 表示这个变量的值只能是这6个字符串之一
export type RepoStatus =
  | 'pending' // 排队中:任务刚入队,worker还没开始执行
  | 'cloning' // 克隆中:worker正在执行git clone
  | 'splitting' // 切分中:正在用tree-sitter AST切分代码块
  | 'embedding'  // 向量化中:正在把代码块向量化写入pgvector
  | 'done' // 已完成:解析成功,可以开始问答
  | 'failed' // 失败:解析过程中出错

// 仓库对象接口:与后端RepoResp响应模型字段一一对齐 
// interface定义对象的形状(有哪些字段、每个字段什么类型)
export interface Repo{
  id:number // 自增主键,数据库自动生成
  repo_url:string // Git仓库HTTPS地址
  collection:string // PGVector的collection名,用于区分不同仓库的向量数据
  status:RepoStatus // 当前解析状态
  error:string | null // 失败时的错误信息,成功时为null
  created_at:string // 创建时间,ISO 格式字符串如"2026-10-28T10:00:00+08:00"
  updated_at:string // 最后更新时间,ISO格式字符串
}

// 引用来源接口:与后端/chat/stream返回的sources数组元素对齐
export interface Source{
  file_path:string // 相对仓库根目录的文件路径,如"requests/models.py"
  start_line:number // 代码块在原文件中的起始行号(1-based)
  end_line:number // 代码块在原文件中的结束行号
  symbol:string | null // 函数名/类名,如果是模块级代码则为null
  content:string // 代码块前200字符摘要,用于前端预览
}

// 聊天消息接口:前端渲染用的数据结构
export interface ChatMessage{
  id:string // 前端生成的唯一ID,用Date.now()+随机数拼接
  role:'user' | 'assistant' // 消息角色:用户消息或AI消息
  content:string // 文本内容:用户的问题,或AI的回答全文
  sources?:Source[] 
  // AI回答附带的引用来源列表
  // ?:表示可选字段,可以不存在
  done?:boolean // 流式生成是否已结束,true=完成,false=生成中
  error?:string // 生成失败时的错误信息
}
