/* 登录页组件,输入管理员密码调用/auth/login换取JWT,成功后把token交给App。*/

/* ============================================================
 * Login.tsx——整个应用的"门卫"
 * 项目中的作用:
 * App.tsx检查到没有token时渲染本组件;
 * 用户输入密码->调后端/auth/login->拿到JWT后通过onLogin回调交还给App;
 * App的token状态一变,自动把本组件替换成仓库列表页。
 * 职责边界:只管「收集密码、调接口、报错误」,
 * token存哪(App管)、登录后去哪(App管)、后续请求怎么带token(client.ts拦截器管)都不归它管。
 * ============================================================ */

// 从react解构导入useState(本组件唯一需要的Hook) 
// 作用:管理三个表单状态——密码、加载中、错误提示 
// 登录页不订阅SSE、不发自动请求,所以不需要useEffect
import {useState} from 'react'
// 从上一级目录的api/client.ts导入封装好的login函数 
// 作用:组件不用自己写axios/拼URL/解析JSON,直接await login(密码)就能拿到token
import {login} from '../api/client'

// =====Props接口:本组件与父组件App之间的"合同"===== 
// 对应App.tsx里:<Login onLogin={(t)=>setToken(t)}/> 
// 数据流向:子组件拿到token后,通过这个回调把数据"举"给父组件(状态提升) 
// 为什么token必须存在App?因为只有App能决定登录后切换到哪个页面,Login自己说了不算
interface LoginProps{
  // onLogin:登录成功回调,接收token字符串,无返回值(void)
  onLogin:(token:string)=>void
  // onCancel:返回访客演示的回调(可选)。
  // 访客模式的改动点:登录页不再是"必经之路",而是访客自己点进来的;
  // 所以必须给一条退路,否则用户点错了就只能关网页
  onCancel?:()=>void
}

// =====组件定义===== 
// {onLogin}:参数位置直接解构props对象,取出onLogin属性 
// LoginProps:类型标注,如果App忘传onLogin,TS在App.tsx那边编译期就报错
export default function Login({onLogin,onCancel}:LoginProps){

  // =====三个状态,对应登录页的三件事=====

  // password:输入框当前显示的文字(受控组件的数据源)
  const [password,setPassword]=useState('')
  // loading:是否正在等后端响应。控制按钮文字("登录"/"登录中...")和输入框禁用 
  // 作用:防止用户狂点造成重复请求
  const [loading,setLoading]=useState(false)
  // error:登录失败的红色提示文字。空串=不显示
  const [error,setError]=useState('')

  // =====表单提交处理:点登录按钮(或回车)后的全部业务逻辑===== 
  // async:内部要await后端响应 
  // e:React.FormEvent:浏览器传给事件处理函数的表单事件对象
  const handleSubmit=async (e:React.FormEvent)=>{
    // 阻止浏览器默认的表单提交行为(整页刷新+跳转) 
    // 我们是单页应用,必须AJAX悄悄发请求,页面一刷新React状态全丢
    e.preventDefault()

    // 前端第一道校验:空密码(或纯空格)直接拦截,不值得发一次网络请求 
    // 注意:这只是体验优化,真正的密码校验在后端——前端校验永远可以被绕过
    if(!password.trim()){ // 若password.trim()=空字符串,则为假,加上!取反为真
    setError('请输入密码')
    return // 提前退出,后面的登录请求不会发
    }

    setLoading(true) // 进入"请求中":按钮禁用+文字变"登录中..."
    setError('') // 清掉上一次的失败提示,避免旧红字残留误导

    try{
      // 成功路径:调client.ts封装的login()->POST/auth/login 
      // await挂起等待后端响应(不阻塞页面,JS靠事件循环调度)
      const token=await login(password.trim())
      // 拿到token后交还给App:App的setToken执行->状态变化->重新渲染 
      // ->App里if(!token)不再成立->本组件被替换成RepoList仓库列表页
      onLogin(token)
    }catch (err:any){
      // 失败路径分两种,用双层可选链+||兜底统一处理: 
      // 1) 密码错:后端返回401{"detail":"..."},err.response.data.detail能取到 
      // 2) 断网/后端挂了:response根本不存在,?.短路成undefined,显示||后面的兜底文案
      setError(err.response?.data?.detail || '登陆失败,请检查网络')
    }finally{
      // finally:不管成功失败都会执行 
      // 必须在这里恢复按钮,否则失败后按钮永远禁用,用户没法重试
      setLoading(false)
    }
  }

  // =====JSX:返回要渲染的UI=====
  return(
    // login-container:对应index.css里的全屏渐变背景+flex居中样式
    <div className="login-container">
      <div className="login-box">
        <h1>RepoTalk</h1>
        <p className="subtitle">开源仓库代码问答Agent</p>

        {/* 用<form>而不是div+按钮onClick的两个好处:
          1) 回车键自动触发提交  2) 浏览器原生表单语义,可访问性好 */}
        <form onSubmit={handleSubmit}>
          {/* 受控组件:输入框显示什么完全由password state决定
              数据环:用户敲键盘->onChange->setPassword更新state
                     ->React重新渲染->value={password}把新值写回输入框
              好处:React永远知道表单当前值,校验/清空/禁用都容易实现 */}
          <input
            type="password" // 输入显示为圆点,防偷窥
            placeholder="请输入管理员密码" // 灰色占位提示,有输入就消失
            value={password} // 显示值=state(只读绑定)
            onChange={(e)=>setPassword(e.target.value)} // 每次击键同步进state
            disabled={loading} // 请求中禁止再输入
            autoFocus // 页面加载完光标自动落在这里
          />

          {/* 条件渲染,利用&&短路特性:
              error是空串(falsy)->整个表达式短路为'',React什么都不渲染
              error有内容(truthy)->渲染右边的红字div */}
          {error && <div className="error">{error}</div>}

          {/* 提交按钮:点击或回车都会触发form的onSubmit */}
          <button type="submit" disabled={loading}>
            {/* 三元表达式:按钮文字随loading状态自动切换,让用户感知系统在工作 */}
            {loading?'登陆中...':'登录'}
          </button>
        </form>

        {/* 返回访客演示:只有 App 真的传了 onCancel 才渲染。
            && 短路:onCancel 是 undefined 时整个表达式为假,不渲染任何东西 */}
        {onCancel && (
          <button
            type="button"
            className="cancel-btn"
            // 点击回调直接执行,不需要包箭头函数(它本身就是一个无参函数)
            onClick={onCancel}
            disabled={loading}
          >
            返回访客演示
          </button>
        )}
      </div>
    </div>
  )
}

