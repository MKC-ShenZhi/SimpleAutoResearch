import { useMemo } from 'react';
import { marked } from 'marked';

marked.setOptions({
  breaks: true,
  gfm: true,
});

interface MarkdownViewProps {
  content: string;
  className?: string;
}

export function MarkdownView({ content, className = '' }: MarkdownViewProps) {
  const html = useMemo(() => {
    try {
      const raw = marked.parse(content || '', { async: false }) as string;
      // Strip raw script tags as a basic safety measure
      return raw.replace(/<script[\s\S]*?>[\s\S]*?<\/script>/gi, '');
    } catch {
      return content;
    }
  }, [content]);

  return <div className={`sar-prose ${className}`} dangerouslySetInnerHTML={{ __html: html }} />;
}
