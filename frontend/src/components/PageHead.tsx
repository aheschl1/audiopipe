import type { ReactNode } from "react"

/** A page's title row: name, a quiet summary line, and page-level actions on the right. */
export default function PageHead({
  title,
  meta,
  children,
}: {
  title: string
  meta?: ReactNode
  children?: ReactNode
}) {
  return (
    <header className="page-head">
      <div className="page-head-text">
        <h1 className="page-title">{title}</h1>
        {meta != null && <div className="page-meta">{meta}</div>}
      </div>
      {children && <div className="page-actions">{children}</div>}
    </header>
  )
}
