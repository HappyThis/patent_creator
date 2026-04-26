import { RefObject, useEffect } from 'react';
import { RenderAst } from '../../types';
import { renderPreviewNodes } from './renderPreviewNodes';

type PreviewPanelProps = {
  renderAst: RenderAst;
  activeSectionId: string;
  activeBlockId: string | null;
  recentSectionIds: string[];
  recentBlockIds: string[];
  previewRef: RefObject<HTMLDivElement>;
  onActiveSectionChange: (sectionId: string) => void;
};

export function PreviewPanel({
  renderAst,
  activeSectionId,
  activeBlockId,
  recentSectionIds,
  recentBlockIds,
  previewRef,
  onActiveSectionChange,
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

  useEffect(() => {
    const container = previewRef.current;
    if (!container) {
      return;
    }

    const updateActiveSection = () => {
      const sections = Array.from(
        container.querySelectorAll<HTMLElement>('.preview-section[data-anchor]'),
      );
      if (sections.length === 0) {
        return;
      }

      const containerTop = container.getBoundingClientRect().top;
      let currentSectionId = sections[0].dataset.anchor ?? null;

      for (const section of sections) {
        const offset = section.getBoundingClientRect().top - containerTop;
        if (offset <= 120) {
          currentSectionId = section.dataset.anchor ?? currentSectionId;
        } else {
          break;
        }
      }

      if (currentSectionId) {
        onActiveSectionChange(currentSectionId);
      }
    };

    updateActiveSection();
    container.addEventListener('scroll', updateActiveSection, { passive: true });
    return () => container.removeEventListener('scroll', updateActiveSection);
  }, [onActiveSectionChange, previewRef, renderAst]);

  return (
    <section className="preview-pane">
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
