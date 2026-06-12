import type { RenderAst, RenderNode, SectionNode } from '../../types';
import type { DocumentStats } from '../preview/documentStats';
import { renderPreviewNodes } from '../preview/renderPreviewNodes';

type InnovationKernelPanelProps = {
  renderAst: RenderAst;
  recentSectionIds: string[];
  recentBlockIds: string[];
  stats: DocumentStats;
};

function findInnovationSection(nodes: RenderNode[]): SectionNode | null {
  for (const node of nodes) {
    if (node.type !== 'section') {
      continue;
    }
    if (
      node.section_type === 'key_innovations' ||
      node.title.includes('关键创新') ||
      node.title.includes('创新内核')
    ) {
      return node;
    }
    const child = findInnovationSection(node.children);
    if (child) {
      return child;
    }
  }
  return null;
}

export function InnovationKernelPanel({
  renderAst,
  recentSectionIds,
  recentBlockIds,
  stats,
}: InnovationKernelPanelProps) {
  const innovationSection = findInnovationSection(renderAst.children);

  return (
    <section className="kernel-pane" aria-label="创新内核预览">
      <header className="kernel-header">
        <div>
          <div className="kernel-title">创新内核</div>
          <div className="kernel-subtitle">{renderAst.title}</div>
        </div>
        <span className="kernel-progress">{stats.filledSections}/{stats.totalSections}</span>
      </header>

      <div className="kernel-scroll">
        {innovationSection ? (
          <article className="kernel-document">
            {renderPreviewNodes({
              nodes: [innovationSection],
              recentSectionIds,
              recentBlockIds,
              sectionStatusById: stats.sectionStatusById,
            })}
          </article>
        ) : (
          <div className="kernel-empty">暂无创新内核内容</div>
        )}
      </div>
    </section>
  );
}
