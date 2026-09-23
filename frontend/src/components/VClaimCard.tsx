import { motion } from 'framer-motion'
import { ShieldCheck, ShieldAlert, ShieldQuestion, Link2 } from 'lucide-react'
import type { Claim } from '../types'
import { useExpertStore } from '../store/expertStore'

const CONF_META = {
  high: { label: '高可信', cls: 'bg-ok/15 text-ok', icon: ShieldCheck },
  medium: { label: '中可信', cls: 'bg-warn/15 text-warn', icon: ShieldCheck },
  low: { label: '低可信', cls: 'bg-risk/15 text-risk', icon: ShieldAlert },
  unverified: { label: '待验证', cls: 'bg-ink-3/15 text-ink-3', icon: ShieldQuestion },
} as const

/** 论点卡：结论文本 + 置信度徽标 + 交叉验证标记 + 证据引用数 + 作者。 */
export function VClaimCard({
  claim,
  onCite,
}: {
  claim: Claim
  onCite?: (evidenceIds: string[]) => void
}) {
  const byId = useExpertStore((s) => s.byId)
  const meta = CONF_META[claim.confidence] ?? CONF_META.unverified
  const Icon = meta.icon
  const author = byId(claim.author)
  const independent = claim.independent_verification
  const independentLabel = independent && ({
    not_verified: '未完成核验', single_authority: '单一权威来源', corroborated: '已独立交叉验证',
    composite_support: '多源联合支持（未独立交叉验证）', same_origin: '多条同源证据', single_source: '单一来源（未独立核验）',
  })[independent.status]
  return (
    <motion.div
      initial={{ opacity: 0, y: 8 }}
      animate={{ opacity: 1, y: 0 }}
      className="rounded-card border border-line/60 bg-card p-4 shadow-card"
    >
      <p className="text-body text-ink">{claim.text}</p>
      {claim.verification && (
        <details className="mt-2 text-tag text-ink-2">
          <summary className="cursor-pointer">
            证据核验：{({ supported: '有原文支持', partial: '部分支持', contradicted: '存在矛盾', insufficient: '依据不足' })[claim.verification.verdict]}
          </summary>
          <p className="mt-2">{claim.verification.reason}</p>
          {claim.verification.assessment?.reason && <p className="mt-1 text-ink-3">范围与时效：{claim.verification.assessment.reason}</p>}
          {claim.verification.supports.map((s, i) => (
            <blockquote key={`${s.evidence_id}-${i}`} className="mt-2 border-l-2 border-line pl-2">
              {s.quote}
              <button className="ml-2 underline" onClick={() => onCite?.([s.evidence_id])}>查看来源</button>
            </blockquote>
          ))}
        </details>
      )}
      <div className="mt-3 flex flex-wrap items-center gap-2">
        <span title={claim.confidence_reason ?? '旧版评级，尚未按新的双维度规则重新核验'} className={`inline-flex items-center gap-1 rounded-chip px-2.5 h-6 text-tag font-medium ${meta.cls}`}>
          <Icon size={12} /> {claim.confidence_policy_version || claim.confidence === 'unverified' ? meta.label : `旧版${meta.label}`}
        </span>
        {(independentLabel || claim.cross_validated) && (
          <span title={independent ? `${independent.evidence_count} 条证据，${independent.source_count} 个原始来源组，其中 ${independent.full_support_source_count} 组各自完整支持结论` : '旧版标记，未按新规则重核'} className="inline-flex items-center gap-1 rounded-chip bg-primary-tint px-2.5 h-6 text-tag font-medium text-primary-deep">
            {independentLabel ?? '旧版跨源标记'}
          </span>
        )}
        {claim.evidence_ids.length > 0 ? (
          <button
            onClick={() => onCite?.(claim.evidence_ids)}
            className="inline-flex items-center gap-1 rounded-chip border border-line px-2.5 h-6 text-tag text-ink-2 transition-colors hover:bg-primary-tint hover:text-primary-deep"
          >
            <Link2 size={12} /> {claim.evidence_ids.length} 条证据
          </button>
        ) : (
          <span className="text-tag text-ink-3">无证据引用</span>
        )}
        {author && (
          <span className="ml-auto inline-flex items-center gap-1.5 text-tag text-ink-3">
            <img src={author.avatar} alt={author.name} className="h-4 w-4 rounded-full object-cover" />
            {author.name}
          </span>
        )}
      </div>
      {claim.confidence_reason && <p className="mt-2 text-tag leading-relaxed text-ink-3">{claim.confidence_reason}</p>}
    </motion.div>
  )
}
