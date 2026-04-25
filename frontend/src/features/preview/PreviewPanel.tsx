import { RefObject, useEffect } from 'react';
import { RenderAst, RenderNode } from '../../types';
import { renderPreviewNodes } from './renderPreviewNodes';

type PreviewPanelProps = {
  renderAst: RenderAst;
  activeSectionId: string;
  activeBlockId: string | null;
  recentSectionIds: string[];
  recentBlockIds: string[];
  previewRef: RefObject<HTMLDivElement>;
};

function collectSectionIds(nodes: RenderNode[]): string[] {
  return nodes.flatMap((node) =>
    node.type === 'section' ? [node.id, ...collectSectionIds(node.children)] : [],
  );
}

export function PreviewPanel({
  renderAst,
  activeSectionId,
  activeBlockId,
  recentSectionIds,
  recentBlockIds,
  previewRef,
}: PreviewPanelProps) {
  useEffect(() => {
    if (!previewRef.current) {
      return;
    }

    const selector = activeBlockId
      ? `[data-block-id="${activeBlockId}"]`
      : `[data-anchor="${activeSectionId}"]`;
    const element = previewRef.current.querySelector(selector);
    if (element instanceof HTMLElement) {
      element.scrollIntoView({ behavior: 'smooth', block: 'center' });
    }
  }, [activeBlockId, activeSectionId, previewRef]);

  return (
    <section className="panel preview-panel">
      <div className="panel-header">
        <h2>交底书预览</h2>
        <span>{collectSectionIds(renderAst.children).length} nodes</span>
      </div>
      <div className="preview-scroll" ref={previewRef}>
        <article className="document-card">
          <div className="document-meta">
            <span>{renderAst.meta.document_type}</span>
            <span>schema {renderAst.meta.schema_version}</span>
          </div>
          <h1>{renderAst.title}</h1>
          {renderPreviewNodes({
            nodes: renderAst.children,
            recentSectionIds,
            recentBlockIds,
          })}
        </article>
      </div>
    </section>
  );
}
