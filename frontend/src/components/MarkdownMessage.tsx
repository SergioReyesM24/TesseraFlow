import ReactMarkdown from 'react-markdown'

interface MarkdownMessageProps {
  content: string
  className?: string
}

/** Render model-authored Markdown without enabling embedded raw HTML. */
export function MarkdownMessage({ content, className }: MarkdownMessageProps) {
  return (
    <div className={className ? `markdown-message ${className}` : 'markdown-message'}>
      <ReactMarkdown>{content}</ReactMarkdown>
    </div>
  )
}
