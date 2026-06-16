import { ReactElement, ReactNode, isValidElement } from 'react';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import remarkMath from 'remark-math';
import rehypeKatex from 'rehype-katex';
import katex from 'katex';

type MarkdownContentProps = {
  children: string;
};

function isMathLanguage(value: unknown): boolean {
  const match = /language-([A-Za-z0-9_-]+)/.exec(typeof value === 'string' ? value : '');
  const language = match?.[1]?.toLowerCase();
  return language === 'latex' || language === 'math' || language === 'tex';
}

function renderFormula(latex: string): ReactNode {
  try {
    const html = katex.renderToString(latex, {
      displayMode: true,
      throwOnError: true,
      strict: false,
    });
    return <div className="markdown-math-block" dangerouslySetInnerHTML={{ __html: html }} />;
  } catch {
    return <code>{latex}</code>;
  }
}

export function MarkdownContent({ children }: MarkdownContentProps) {
  return (
    <ReactMarkdown
      remarkPlugins={[remarkGfm, remarkMath]}
      rehypePlugins={[rehypeKatex]}
      components={{
        a: ({ node: _node, ...props }) => (
          <a {...props} target="_blank" rel="noopener noreferrer" />
        ),
        pre: ({ node: _node, children, ...props }) => {
          const child = Array.isArray(children) ? children[0] : children;
          if (isValidElement(child) && isMathLanguage((child as ReactElement<{ className?: string }>).props.className)) {
            return <>{children}</>;
          }
          return <pre {...props}>{children}</pre>;
        },
        code: ({ node: _node, className, children, ...props }) => {
          if (isMathLanguage(className)) {
            const latex = String(children).replace(/\n$/, '');
            return renderFormula(latex);
          }
          return (
            <code className={className} {...props}>
              {children}
            </code>
          );
        },
      }}
    >
      {children}
    </ReactMarkdown>
  );
}
