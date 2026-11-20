import { useMemo } from 'react';
import { marked } from 'marked';
import katex from 'katex';
import 'katex/dist/katex.min.css';

marked.setOptions({
  breaks: true,
  gfm: true,
});

interface MarkdownViewProps {
  content: string;
  className?: string;
}

function renderWithMath(source: string): string {
  const placeholders: string[] = [];
  const stash = (html: string) => {
    const idx = placeholders.length;
    placeholders.push(html);
    return `@@SAR_MATH_${idx}@@`;
  };

  // 1. Protect fenced code blocks and inline code spans from math parsing
  let text = source.replace(/```[\s\S]*?```/g, (block) => stash(block));
  text = text.replace(/`[^`\n]+`/g, (inline) => stash(inline));

  // 2. Display math: $$...$$ and \[...\]
  text = text.replace(/\$\$([\s\S]+?)\$\$/g, (_m, expr: string) => {
    const rendered = katex.renderToString(expr.trim(), {
      displayMode: true,
      throwOnError: false,
      strict: false,
    });
    return `\n\n${stash(`<div class="my-3 overflow-x-auto py-1">${rendered}</div>`)}\n\n`;
  });

  text = text.replace(/\\\[([\s\S]+?)\\\]/g, (_m, expr: string) => {
    const rendered = katex.renderToString(expr.trim(), {
      displayMode: true,
      throwOnError: false,
      strict: false,
    });
    return `\n\n${stash(`<div class="my-3 overflow-x-auto py-1">${rendered}</div>`)}\n\n`;
  });

  // 3. Inline math: $...$ (avoid matching currency like $100) and \(...\)
  text = text.replace(/(^|[^\\$])\$([^\n$]+?)\$/g, (match, prefix: string, expr: string) => {
    const trimmed = expr.trim();
    if (!trimmed || /^\d[\d,.\s]*$/.test(trimmed)) return match;
    const rendered = katex.renderToString(trimmed, {
      displayMode: false,
      throwOnError: false,
      strict: false,
    });
    return `${prefix}${stash(rendered)}`;
  });

  text = text.replace(/\\\(([^\n]+?)\\\)/g, (_m, expr: string) => {
    const rendered = katex.renderToString(expr.trim(), {
      displayMode: false,
      throwOnError: false,
      strict: false,
    });
    return stash(rendered);
  });

  // 4. Restore stashed code blocks before marked.parse, keep math stashed until after
  const codeRestored = text.replace(/@@SAR_MATH_(\d+)@@/g, (token, i) => {
    const val = placeholders[Number(i)];
    return val.startsWith('`') ? val : token;
  });

  let html = marked.parse(codeRestored, { async: false }) as string;

  // 5. Restore rendered KaTeX HTML
  html = html.replace(/@@SAR_MATH_(\d+)@@/g, (_token, i) => placeholders[Number(i)] || '');

  // Basic script tag safety
  return html.replace(/<script[\s\S]*?>[\s\S]*?<\/script>/gi, '');
}

export function MarkdownView({ content, className = '' }: MarkdownViewProps) {
  const html = useMemo(() => {
    try {
      return renderWithMath(content || '');
    } catch {
      return content;
    }
  }, [content]);

  return <div className={`sar-prose ${className}`} dangerouslySetInnerHTML={{ __html: html }} />;
}
