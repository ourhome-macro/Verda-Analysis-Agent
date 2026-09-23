import type { Report } from '../types'

export default function VResearchMatrix({ report }: { report: Report }) {
  const matrix = report.research_matrix
  if (!matrix) return null
  const labels: Record<string, string> = { covered: '已覆盖', background_only: '仅背景/日期不明', missing: '缺少核验事实' }
  const claims = new Map(report.claims.map(c => [c.claim_id, c]))
  const times: Record<string, string> = {recent_month: '近一月来源', recent_quarter: '近季度来源', background: '背景资料', unknown: '发布日期未知', future: '日期异常'}
  return (
    <section data-testid="research-matrix" className="mb-8 rounded-card border border-line bg-card p-5">
      <h2 className="font-serif text-xl font-semibold">品牌 × 研究维度</h2>
      <p className="mt-2 text-aux text-ink-2">事实覆盖 {matrix.fact_covered}/{matrix.total} 格 · 满足时效 {matrix.covered}/{matrix.total} 格。截止 {matrix.contract.as_of.slice(0, 10)}；{matrix.contract.window_days === 30 ? '优先近一月，背景资料不冒充近期变化。' : '按研究窗口检索；未注明日期不代表最新。'}</p>
      <div className="mt-4 overflow-x-auto">
        <table className="w-full text-left text-aux">
          <thead><tr><th className="p-2">品牌</th>{matrix.contract.dimensions.map(d => <th key={d.key} className="p-2">{d.label}</th>)}</tr></thead>
          <tbody>{matrix.contract.brands.map(brand => <tr key={brand} className="border-t border-line">
            <th className="p-2 align-top">{brand}</th>
            {matrix.contract.dimensions.map(d => {
              const cell = matrix.cells.find(c => c.brand === brand && c.dimension === d.key)
              return <td key={d.key} className="min-w-40 p-2 align-top">
                <details><summary className={cell?.status === 'covered' ? 'cursor-pointer text-primary-deep' : 'cursor-pointer text-risk'}>
                  {labels[cell?.status ?? 'missing']} · {cell?.claim_ids.length ?? 0} 条
                </summary>
                  <p className="mt-1 text-tag text-ink-2">高可信 {cell?.high_count ?? 0} 条；{cell?.gap || '点击来源可查看原始证据'}</p>
                  {cell?.claim_ids.map(id => {
                    const claim = claims.get(id)
                    return claim && <div key={id} className="mt-2 border-t border-line pt-2">
                      <p>{claim.text}</p>
                      <p className="text-tag text-ink-2">{times[claim.temporal?.label ?? 'unknown']}</p>
                      {claim.evidence_ids.map(eid => {
                        const evidence = report.evidence.find(e => e.evidence_id === eid)
                        return evidence && <a key={eid} href={evidence.source_url} target="_blank" rel="noreferrer" className="mr-2 text-tag text-primary underline" title={`发布：${evidence.published_at || '未知'}；采集：${evidence.captured_at}`}>来源</a>
                      })}
                    </div>
                  })}
                </details>
              </td>
            })}
          </tr>)}</tbody>
        </table>
      </div>
      <details className="mt-4 text-aux"><summary className="cursor-pointer">近一月来源事实清单（{matrix.recent_claim_ids.length} 条）</summary>
        <p className="mt-2 text-ink-2">此标签仅表示支持来源发表于近一月，不代表产品在近一月新增该能力。</p>
        {matrix.recent_claim_ids.map(id => <p key={id} className="mt-2">{claims.get(id)?.text}</p>)}
      </details>
    </section>
  )
}
