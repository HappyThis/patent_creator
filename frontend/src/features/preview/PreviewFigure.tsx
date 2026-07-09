import { useEffect, useState } from 'react';
import { FigureDrawioEditorModal } from '../figure-editor/FigureDrawioEditorModal';
import { API_BASE_URL } from '../../services/api/http';
import { FigureRenderAsset } from '../../types';

type PreviewFigureProps = {
  figure?: FigureRenderAsset;
  figureId: string;
  projectId: string | null;
  onFigureSaved: () => void;
};

export function PreviewFigure({ figure, figureId, projectId, onFigureSaved }: PreviewFigureProps) {
  const [expanded, setExpanded] = useState(false);
  const [editing, setEditing] = useState(false);
  const [loadFailed, setLoadFailed] = useState(false);
  const renderUrl = figure?.render?.type === 'png'
    ? withCacheKey(toAssetUrl(figure.render.url || ''), figure.source?.updated_at)
    : '';
  const title = figure ? `${figure.label} ${figure.title}`.trim() : figureId;
  const canEdit = Boolean(projectId && figure?.source?.type === 'drawio');

  useEffect(() => {
    setLoadFailed(false);
  }, [renderUrl]);

  useEffect(() => {
    if (!expanded) {
      return;
    }
    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') {
        setExpanded(false);
      }
    };
    window.addEventListener('keydown', handleKeyDown);
    return () => window.removeEventListener('keydown', handleKeyDown);
  }, [expanded]);

  if (!figure) {
    return <div className="preview-figure-missing">附图未生成</div>;
  }
  if (!renderUrl) {
    return <div className="preview-figure-missing">附图截图缺失</div>;
  }
  if (loadFailed) {
    return <div className="preview-figure-missing">附图截图加载失败</div>;
  }

  return (
    <>
      <div className="preview-figure-svg">
        <button
          type="button"
          className="preview-figure-expand"
          aria-label="展开预览附图"
          title="展开预览"
          onClick={() => setExpanded(true)}
        >
          <svg viewBox="0 0 16 16" aria-hidden="true">
            <path d="M5.6 2H2v3.6M10.4 2H14v3.6M14 10.4V14h-3.6M2 10.4V14h3.6" />
            <path d="M2.4 2.4 6 6M13.6 2.4 10 6M13.6 13.6 10 10M2.4 13.6 6 10" />
          </svg>
        </button>
        {canEdit ? (
          <button
            type="button"
            className="preview-figure-edit"
            aria-label="编辑附图"
            title="编辑附图"
            onClick={() => setEditing(true)}
          >
            <svg viewBox="0 0 16 16" aria-hidden="true">
              <path d="M3 11.7V14h2.3L12.1 7.2 9.8 4.9 3 11.7Z" />
              <path d="m9.2 5.5 1.1-1.1a1.4 1.4 0 0 1 2 0l.3.3a1.4 1.4 0 0 1 0 2l-1.1 1.1" />
            </svg>
          </button>
        ) : null}
        <div className="preview-figure-svg-content">
          <img className="preview-figure-image" src={renderUrl} alt={title} onError={() => setLoadFailed(true)} />
        </div>
      </div>
      {expanded ? (
        <div className="preview-figure-lightbox" role="dialog" aria-modal="true" aria-label="附图预览" onClick={() => setExpanded(false)}>
          <div className="preview-figure-lightbox-panel" onClick={(event) => event.stopPropagation()}>
            <div className="preview-figure-lightbox-toolbar">
              <span>{title}</span>
              <button type="button" className="preview-figure-lightbox-close" aria-label="关闭附图预览" onClick={() => setExpanded(false)}>
                <svg viewBox="0 0 16 16" aria-hidden="true">
                  <path d="m4 4 8 8M12 4l-8 8" />
                </svg>
              </button>
            </div>
            <div className="preview-figure-lightbox-body">
              <img className="preview-figure-lightbox-image" src={renderUrl} alt={title} />
            </div>
          </div>
        </div>
      ) : null}
      {editing && projectId && canEdit ? (
        <FigureDrawioEditorModal
          projectId={projectId}
          figureId={figureId}
          onClose={() => setEditing(false)}
          onSaved={onFigureSaved}
        />
      ) : null}
    </>
  );
}

function withCacheKey(url: string, cacheKey: string | undefined): string {
  if (!url || !cacheKey) {
    return url;
  }
  const separator = url.includes('?') ? '&' : '?';
  return `${url}${separator}v=${encodeURIComponent(cacheKey)}`;
}

function toAssetUrl(url: string): string {
  if (!url || /^https?:\/\//i.test(url)) {
    return url;
  }
  if (url.startsWith('/')) {
    return `${API_BASE_URL.replace(/\/$/, '')}${url}`;
  }
  return url;
}
