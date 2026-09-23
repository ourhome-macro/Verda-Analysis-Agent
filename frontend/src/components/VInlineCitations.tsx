import { Fragment } from 'react'

/** Keep stored evidence IDs intact, display compact clickable reference numbers. */
export function VInlineCitations({ text, numbers, onCite }: {
  text: string
  numbers: Map<string, number>
  onCite: (ids: string[]) => void
}) {
  return <>{text.split(/(\[e_[^\]]+\])/g).map((part, i) => {
    const ids = part.startsWith('[e_') ? (part.match(/e_[a-zA-Z0-9]+/g) ?? []).filter((id) => numbers.has(id)) : []
    return ids.length ? <button key={i} type="button" onClick={() => onCite(ids)}
      title={`查看来源 ${ids.map((id) => numbers.get(id)).join('、')}`}
      className="mx-0.5 inline rounded bg-primary-tint px-1 text-tag text-primary-deep hover:underline">
      [{ids.map((id) => numbers.get(id)).join(', ')}]
    </button> : <Fragment key={i}>{part}</Fragment>
  })}</>
}
