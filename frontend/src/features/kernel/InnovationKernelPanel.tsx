import { MarkdownContent } from '../../components/MarkdownContent';
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
            <MarkdownContent>{markdown}</MarkdownContent>
          </article>
        ) : (
          <div className="kernel-empty">暂无内容</div>
        )}
      </div>
    </section>
  );
}
