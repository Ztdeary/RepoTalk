/* 访客自带 LLM Key 的设置入口:一个按钮 + 一个弹窗。
   功能:读配置 -> 展示预设 -> 测试连接 -> 存进 sessionStorage -> 可清除。*/

import { useState, useEffect } from 'react'
// useState:弹窗开关/表单字段/加载状态
// useEffect:组件挂载时读一次站点配置和本地是否已有Key

import { getSiteConfig, testLLM, type SiteConfig } from '../api/client'
// getSiteConfig:GET /demo/config,决定本组件是否渲染
// testLLM:POST /demo/test-llm,先测通再保存
// SiteConfig:配置类型

import { clearCred, hasCred, loadCred, saveCred } from '../api/session'
// Key 的读/存/清/是否存在

interface ApiSettingsProps {
  // onChanged:保存或清除成功后通知父组件。
  // 为什么需要:父组件(列表页)要根据"现在有没有Key"来显示/隐藏
  // "提交仓库需要先配置Key"的提示,不通知就只能是过期状态
  onChanged?: () => void
}

// 预设服务商:地址 + 默认模型。
// 为什么给预设:访客大概率不知道 OpenAI 兼容地址怎么写,
// 点一下就能填好,少一次"填错->测试失败->放弃"的流失
type Preset = { name: string; baseUrl: string; model: string }
const PRESETS: Preset[] = [
  { name: 'DeepSeek', baseUrl: 'https://api.deepseek.com', model: 'deepseek-chat' },
  { name: 'OpenAI', baseUrl: 'https://api.openai.com/v1', model: 'gpt-4o-mini' },
  { name: 'Kimi', baseUrl: 'https://api.moonshot.cn/v1', model: 'moonshot-v1-8k' },
  { name: '智谱', baseUrl: 'https://open.bigmodel.cn/api/paas/v4', model: 'glm-4-flash' },
  { name: '硅基流动', baseUrl: 'https://api.siliconflow.cn/v1', model: 'deepseek-ai/DeepSeek-V3' },
]

export default function ApiSettings({ onChanged }: ApiSettingsProps) {
  // cfg:站点配置。null = 还没拿到,此时整个组件不渲染(避免闪一下又消失)
  const [cfg, setCfg] = useState<SiteConfig | null>(null)
  // open:弹窗是否可见
  const [open, setOpen] = useState(false)
  // 三个表单字段(受控组件)
  const [baseUrl, setBaseUrl] = useState('')
  const [apiKey, setApiKey] = useState('')
  const [model, setModel] = useState('')
  // busy:正在测试连接,期间禁用所有按钮,防止重复提交
  const [busy, setBusy] = useState(false)
  // error:弹窗里的红色错误文案
  const [error, setError] = useState('')
  // saved:本标签页是否已保存Key,决定右上角按钮的颜色和文字
  const [saved, setSaved] = useState(false)

  useEffect(() => {
    // 挂载时先读本地状态:用户可能上个页面刚配过Key
    setSaved(hasCred())
    getSiteConfig()
      .then((c) => setCfg(c))
      // 配置接口失败(后端没起/404)时按"不显示"处理:
      // 宁可不给入口,也不要展示一个点了必然失败的功能
      .catch(() => setCfg(null))
  }, [])

  // 总开关关闭(比如公网 HTTP 阶段)或配置没拿到:整个组件返回 null,什么都不渲染
  // 返回值 null 在 React 里就是"这个组件不存在",不会留下空白占位
  if (!cfg || !cfg.byok_enabled) return null

  const openModal = () => {
    const old = loadCred()
    // 回填优先级:已保存的值 > 第一个预设的默认值
    // 这样"改一个字段再保存"时不用把另外两个重新输一遍
    setBaseUrl(old?.baseUrl ?? PRESETS[0].baseUrl)
    setApiKey(old?.apiKey ?? '')
    setModel(old?.model ?? PRESETS[0].model)
    setError('')
    setOpen(true)
  }

  const applyPreset = (p: Preset) => {
    // 只填地址和模型;Key 必须用户自己粘贴,预设里不可能有别人的Key
    setBaseUrl(p.baseUrl)
    setModel(p.model)
  }

  const handleSave = async () => {
    setError('')
    // trim 后为空就视为没填:全空格等于空
    if (!baseUrl.trim() || !apiKey.trim() || !model.trim()) {
      setError('地址、Key、模型都要填')
      return
    }
    setBusy(true)
    const cred = {
      baseUrl: baseUrl.trim(),
      apiKey: apiKey.trim(),
      model: model.trim(),
    }
    // 先测试再保存:不测就存,用户会在真正提问时才发现Key不能用,
    // 那时报错离"配置"这个动作已经很远,很难联想到是Key的问题
    const res = await testLLM(cred)
    setBusy(false)
    if (!res.ok) {
      setError(res.message || '连接测试失败')
      return
    }
    saveCred(cred)
    setSaved(true)
    setOpen(false)
    // 通知父组件刷新(比如列表页据此收起"请先配置Key"的提示)
    // ?.():可选调用,父组件没传 onChanged 也不会报错
    onChanged?.()
  }

  const handleClear = () => {
    clearCred()
    setSaved(false)
    setApiKey('')
    setOpen(false)
    onChanged?.()
  }

  return (
    // Fragment:把"按钮"和"弹窗"两个兄弟元素一起返回,不额外产生一层 DOM
    <>
      <button
        // 三元:有Key用绿色样式,没Key用普通样式
        className={saved ? 'api-key-on' : 'api-key-btn'}
        onClick={openModal}
        type="button"
        // type="button":必须写!否则按钮在表单里默认是 submit,
        // 点一下会触发表单提交导致页面刷新
      >
        {saved ? '自带 Key 已启用' : '自带 Key'}
      </button>

      {/* 遮罩层:只在 open 时渲染。onClick 挂在遮罩上=点空白处关闭 */}
      {open && (
        <div
          className="modal-mask"
          onClick={() => {
            // 测试中不允许关闭:请求还在飞时把弹窗拆了,
            // 用户会以为"点了没反应",还会重复点
            if (!busy) setOpen(false)
          }}
        >
          <div
            className="modal"
            // stopPropagation:阻止点击事件冒泡到遮罩层,
            // 否则点弹窗内部任何地方都会被当成"点遮罩"而关闭
            onClick={(e) => e.stopPropagation()}
          >
            <h3>使用你自己的 LLM Key</h3>
            <p className="hint">
              Key 只存在本标签页的 sessionStorage,关掉页面即消失;
              后端仅透传给模型服务商,不落库、不写日志。
            </p>

            <div className="preset-row">
              {/* map 渲染预设按钮;key 用服务商名(天然唯一) */}
              {PRESETS.map((p) => (
                <button
                  key={p.name}
                  type="button"
                  className="preset-btn"
                  onClick={() => applyPreset(p)}
                  disabled={busy}
                >
                  {p.name}
                </button>
              ))}
            </div>

            <div>
              {/* label 与 input 未用 htmlFor 关联:这里只作视觉标题,
                  点文字不需要聚焦输入框,省一个 id 属性 */}
              <label>Base URL</label>
              <input
                type="text"
                value={baseUrl}
                // 受控组件数据环:敲字 -> onChange 写 state -> 重渲染 -> value 回填
                onChange={(e) => setBaseUrl(e.target.value)}
                placeholder="https://api.deepseek.com"
                disabled={busy}
              />
            </div>

            <div>
              <label>API Key</label>
              <input
                // type="password":Key 不明文显示在屏幕上,
                // 防止录屏/截图时被旁人看到
                type="password"
                value={apiKey}
                onChange={(e) => setApiKey(e.target.value)}
                placeholder="sk-..."
                disabled={busy}
                // autoComplete="off":别让浏览器把Key记进表单自动填充历史
                autoComplete="off"
              />
            </div>

            <div>
              <label>模型名</label>
              <input
                type="text"
                value={model}
                onChange={(e) => setModel(e.target.value)}
                placeholder="deepseek-chat"
                disabled={busy}
              />
            </div>

            <p className="hint">
              仓库解析与向量化仍使用站主的 embedding 额度,你的 Key 只用于生成回答。
            </p>

            {/* && 短路:error 为空串(falsy)时不渲染 */}
            {error && <div className="error">{error}</div>}

            <div className="modal-actions">
              {/* 只有已保存过才显示"清除",否则用户看到一个永远点不动的按钮 */}
              {saved && (
                <button type="button" className="danger" onClick={handleClear} disabled={busy}>
                  清除
                </button>
              )}
              <button type="button" className="ghost" onClick={() => setOpen(false)} disabled={busy}>
                取消
              </button>
              <button type="button" onClick={handleSave} disabled={busy}>
                {busy ? '测试中...' : '测试并保存'}
              </button>
            </div>
          </div>
        </div>
      )}
    </>
  )
}
