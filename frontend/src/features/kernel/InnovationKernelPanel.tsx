import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import type { InnovationKernelState } from '../../types';

type InnovationKernelPanelProps = {
  innovationKernel: InnovationKernelState;
};

export function InnovationKernelPanel({ innovationKernel }: InnovationKernelPanelProps) {
  const markdown = innovationKernel.kernel_markdown.trim();

  return (
    <section className="kernel-pane" aria-label="创新内核预览">
      <div className="kernel-scroll">
        {markdown ? (
          <article className="kernel-document markdown-body">
            <ReactMarkdown remarkPlugins={[remarkGfm]}>{markdown}</ReactMarkdown>
          </article>
        ) : (
          <div className="kernel-empty">当前 session 暂无创新内核</div>
        )}
      </div>
    </section>
  );
}
