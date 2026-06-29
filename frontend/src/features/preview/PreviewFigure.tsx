import { useEffect, useState } from 'react';
import { FigureRenderAsset } from '../../types';

type PreviewFigureProps = {
  figure?: FigureRenderAsset;
  figureId: string;
};

export function PreviewFigure({ figure, figureId }: PreviewFigureProps) {
  const [expanded, setExpanded] = useState(false);
  const [loadFailed, setLoadFailed] = useState(false);
  const renderUrl = figure?.render?.type === 'png' ? figure.render.url || '' : '';
  const title = figure ? `${figure.label} ${figure.title}`.trim() : figureId;

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
    </>
  );
}
