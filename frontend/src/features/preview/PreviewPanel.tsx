import { RefObject, useEffect } from 'react';
import { RenderAst } from '../../types';
import { PreviewFocusTarget } from '../../hooks/useWorkspaceSelection';
import { DocumentStats } from './documentStats';
import { renderPreviewNodes } from './renderPreviewNodes';

type PreviewPanelProps = {
  renderAst: RenderAst;
  previewFocusTarget: PreviewFocusTarget | null;
  recentSectionIds: string[];
  recentBlockIds: string[];
  stats: DocumentStats;
  previewRef: RefObject<HTMLDivElement>;
  onActiveSectionChange: (sectionId: string) => void;
  onExport: () => void;
};

export function PreviewPanel({
  renderAst,
  previewFocusTarget,
  recentSectionIds,
  recentBlockIds,
  stats,
  previewRef,
  onActiveSectionChange,
  onExport,
}: PreviewPanelProps) {
  const figuresById = Object.fromEntries((renderAst.figures ?? []).map((figure) => [figure.figure_id, figure]));

  useEffect(() => {
    if (!previewRef.current) {
      return;
    }
    if (!previewFocusTarget?.sectionId) {
      return;
    }

    const selector = previewFocusTarget.blockId
      ? `[data-block-id="${previewFocusTarget.blockId}"]`
      : `[data-anchor="${previewFocusTarget.sectionId}"]`;
    const element = previewRef.current.querySelector(selector);
    if (element instanceof HTMLElement) {
      element.scrollIntoView({ behavior: 'smooth', block: 'center' });
    }
  }, [previewFocusTarget, previewRef]);

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
      <button
        className="preview-export-button"
        type="button"
        onClick={onExport}
        aria-label="导出 DOCX"
        title="导出 DOCX"
      >
        <svg className="preview-export-icon" viewBox="0 0 24 24" aria-hidden="true">
          <path d="M12 4v10" />
          <path d="m8.5 10.5 3.5 3.5 3.5-3.5" />
          <path d="M5 19h14" />
        </svg>
      </button>
      <div className="preview-scroll" ref={previewRef}>
        <article className="document-card">
          {renderPreviewNodes({
            nodes: renderAst.children,
            recentSectionIds,
            recentBlockIds,
            sectionStatusById: stats.sectionStatusById,
            figuresById,
          })}
          <footer className="document-status-bar" aria-label="文档状态">
            <span>字数：{stats.characters.toLocaleString('zh-CN')}</span>
            <span>章节：{stats.filledSections}/{stats.totalSections}</span>
            <span>{stats.filledSections === stats.totalSections ? '结构完整' : '待补充'}</span>
          </footer>
        </article>
      </div>
    </section>
  );
}
