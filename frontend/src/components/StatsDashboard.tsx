/* 管理员数据看板:今日 / 近7天 / 近30天的访问与问答统计。
 *
 * 在项目中的作用:
 *   1) 只由管理员看到(RepoList 里判断"是否登录"后才渲染本组件)
 *   2) 切换时间范围时只改 days 参数,后端一次性返回"汇总 + 每日明细"
 *   3) 每 60 秒自动拉一次,不需要手动刷新页面
 *   4) 所有文案中文、数字带千分位,放在页面顶部一眼能看懂
 *
 * 数据为什么可信:
 *   - PV 做了双重去重(前端 sessionStorage + 后端 Redis 30 分钟窗口),
 *     按 F5 不会把访问量刷爆
 *   - 周期的 UV / 提问人数 / 提交人数由后端做集合求并集,
 *     不是"每天的数字相加",所以不会把同一个人重复计数
 */

// useState/useEffect/useCallback/useRef:React 的状态、副作用、函数缓存、可变引用
import {useState,useEffect,useCallback,useRef} from 'react'

// getDemoStats:GET /demo/stats?days=1|7|30(axios 拦截器会自动带管理员 token)
// DemoStats/StatsDay/StatsTotals:后端返回结构的类型定义
import {getDemoStats,type DemoStats} from '../api/client'

// 时间范围选项。
// as const:让 TS 把 days 推断成字面量类型,而不是宽泛的 number
const RANGES=[
  {days:1,label:'今日'},
  {days:7,label:'近 7 天'},
  {days:30,label:'近 30 天'},
] as const

// 后端日期是紧凑格式(20261106),显示时加连字符更好读
function fmtDate(raw:string):string{
  return `${raw.slice(0,4)}-${raw.slice(4,6)}-${raw.slice(6,8)}`
}

// 数字加千分位:12345 -> 12,345,大数字不用数零
function fmtNum(value:number):string{
  return value.toLocaleString('zh-CN')
}

export default function StatsDashboard(){
  // days:当前选中的统计范围(1/7/30),切换 tab 时改它
  const [days,setDays]=useState<number>(1)
  // data:后端返回的完整快照;null 表示还没加载到
  const [data,setData]=useState<DemoStats|null>(null)
  // error:接口错误提示(比如 token 过期)
  const [error,setError]=useState('')
  // loading:只在首次/切换范围时显示"加载中",后台自动刷新不闪
  const [loading,setLoading]=useState(true)
  // updatedAt:上次成功刷新的时刻,让用户知道数据新不新
  const [updatedAt,setUpdatedAt]=useState('')

  // seq:请求序号。用户快速点 7 天->30 天时,7 天的响应可能后到,
  // 不拦住就会用旧数据覆盖新数据(看板显示 30 天却只有 7 天数字)
  const seq=useRef(0)

  // 拉取一次统计数据。
  // useCallback:把函数引用固定下来,下面两个 useEffect 才能安全依赖它
  const load=useCallback(async(d:number)=>{
    const my=++seq.current // 本次请求的序号
    try{
      const res=await getDemoStats(d)
      if(my!==seq.current) return // 已经有更新的请求了,丢弃这次结果
      setData(res)
      setError('')
      // hour12:false -> 24 小时制,符合国内习惯
      setUpdatedAt(new Date().toLocaleTimeString('zh-CN',{hour12:false}))
    }catch(err:any){
      if(my!==seq.current) return
      // FastAPI 错误体是 {"detail":"..."};401 时说明 token 失效
      setError(err.response?.data?.detail || '统计数据加载失败')
    }finally{
      if(my===seq.current) setLoading(false)
    }
  },[])

  // 切换范围时立刻重新拉取
  useEffect(()=>{
    setLoading(true)
    load(days)
  },[days,load])

  // 自动刷新:每 60 秒拉一次当前范围的数据。
  // 依赖 days:切换范围后要重新计时,避免继续请求旧范围
  useEffect(()=>{
    const timer=setInterval(()=>{load(days)},60000)
    // 清理函数:组件卸载或 days 变化时清掉旧定时器,否则会同时跑多个
    return ()=>clearInterval(timer)
  },[days,load])

  const totals=data?.totals
  const daily=data?.daily ?? []

  // 柱状图基准:取范围内最大值,保证最长的柱子正好占满,不会溢出容器
  const peak=Math.max(1,...daily.map(d=>Math.max(d.visits,d.questions)))

  // 顶部指标卡。按"能回答什么问题"分组:
  //   有多少人来 -> 访问/独立访客
  //   用了多少 -> 提问/提问人数
  //   站长花多少 -> 站主买单/自带Key/预计花费
  //   内容供给 -> 提交仓库
  const cards=[
    {label:'访问次数(PV)',value:totals?.visits ?? 0,accent:'pink'},
    {label:'独立访客(UV)',value:totals?.unique_visitors ?? 0,accent:'blue'},
    {label:'提问次数',value:totals?.questions ?? 0,accent:'green'},
    {label:'提问人数',value:totals?.askers ?? 0,accent:'green'},
    {label:'站主买单提问',value:totals?.guest_questions ?? 0,accent:'amber'},
    {label:'自带 Key 提问',value:totals?.byok_questions ?? 0,accent:'purple'},
    {label:'提交仓库数',value:totals?.repos ?? 0,accent:'blue'},
    {label:'预计花费(元)',value:totals?.estimated_guest_cost_yuan ?? 0,accent:'amber',money:true},
  ]

  return(
    <section className="stats-section">
      {/* =====看板头部:标题 + 时间范围切换===== */}
      <div className="stats-head">
        <div>
          <h2>数据看板</h2>
          <p className="stats-sub">
            {data
              ? `统计范围 ${fmtDate(data.period.start)} ~ ${fmtDate(data.period.end)}(北京时间)`
              : '正在读取统计数据...'}
            {updatedAt && ` · 上次更新 ${updatedAt}`}
            {' · 每 60 秒自动刷新'}
          </p>
        </div>

        <div className="stats-tabs">
          {RANGES.map(r=>(
            <button
              key={r.days}
              // 选中态用 class 控制配色;这里不用内联 style,方便 CSS 统一调
              className={`stats-tab${days===r.days?' active':''}`}
              onClick={()=>setDays(r.days)}
            >
              {r.label}
            </button>
          ))}
        </div>
      </div>

      {/* =====错误提示:token 过期或后端异常===== */}
      {error && <div className="error">{error}</div>}

      {/* =====加载中:只在还没有数据时占位,避免整块高度跳动===== */}
      {loading && !data && <p className="stats-note">加载中...</p>}

      {totals && (
        <>
          {/* =====指标卡:8 个数字一屏看完===== */}
          <div className="stats-cards">
            {cards.map(c=>(
              <div className="stat-card" key={c.label}>
                <span className="stat-label">{c.label}</span>
                <span className={`stat-value ${c.accent}`}>
                  {/* 金额保留两位小数,其他整数加千分位 */}
                  {c.money ? `¥ ${Number(c.value).toFixed(2)}` : fmtNum(c.value)}
                </span>
              </div>
            ))}
          </div>

          {/* =====每日明细柱状图:只有多天范围才需要===== */}
          {days>1 && (
            <div className="stats-bars">
              <div className="stats-legend">
                <span><i className="dot visits"/>访问次数</span>
                <span><i className="dot questions"/>提问次数</span>
              </div>

              {daily.map(d=>(
                <div className="bar-row" key={d.date}>
                  <span className="bar-date">{fmtDate(d.date)}</span>
                  <div className="bar-group">
                    {/* width:按"当天数值/范围内峰值"算百分比;数值为 0 时不画 */}
                    <div
                      className="bar visits"
                      style={{width:`${Math.round((d.visits/peak)*100)}%`}}
                      title={`访问 ${d.visits} 次`}
                    />
                    <div
                      className="bar questions"
                      style={{width:`${Math.round((d.questions/peak)*100)}%`}}
                      title={`提问 ${d.questions} 次`}
                    />
                  </div>
                  {/* 数字标签:让柱状图能读数,而不只是看长短 */}
                  <span className="bar-nums">
                    {fmtNum(d.visits)} / {fmtNum(d.questions)}
                  </span>
                </div>
              ))}
            </div>
          )}

          {/* =====口径说明:把"为什么数字是这样"写在页面上,避免误会===== */}
          <p className="stats-note">
            同一访客 30 分钟内只计 1 次访问,刷新页面不会重复计数;
            周期内的独立访客 / 提问人数 / 提交人数已按去重口径合并,不是每日数字相加;
            数据保留 30 天,每天的数据在一个月后自动过期,按北京时间自然日切分。
          </p>
        </>
      )}
    </section>
  )
}
