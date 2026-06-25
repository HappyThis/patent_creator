import { CompositionEvent, KeyboardEvent, useState } from 'react';
import type { ContextUsageSummary, QualityMode } from '../../types';
import { ContextUsageBadge } from './ContextUsageBadge';

type ChatComposerProps = {
  composer: string;
  isBusy: boolean;
  contextUsage?: ContextUsageSummary | null;
  qualityMode: QualityMode;
  canCancel?: boolean;
  isCancelling?: boolean;
  onComposerChange: (value: string) => void;
  onQualityModeChange: (mode: QualityMode) => void;
  onSubmit: () => void;
  onCancel: () => void;
};

export function ChatComposer({
  composer,
  isBusy,
  contextUsage,
  qualityMode,
  canCancel = false,
  isCancelling = false,
  onComposerChange,
  onQualityModeChange,
  onSubmit,
  onCancel,
}: ChatComposerProps) {
  const [isComposing, setIsComposing] = useState(false);

  const handleCompositionStart = (_event: CompositionEvent<HTMLTextAreaElement>) => {
    setIsComposing(true);
  };

  const handleCompositionEnd = (_event: CompositionEvent<HTMLTextAreaElement>) => {
    setIsComposing(false);
  };

  const handleKeyDown = (event: KeyboardEvent<HTMLTextAreaElement>) => {
    if (event.key !== 'Enter' || event.shiftKey) {
      return;
    }
    if (isComposing || event.nativeEvent.isComposing || event.keyCode === 229) {
      return;
    }
    event.preventDefault();
    onSubmit();
  };

  return (
    <div className="composer">
      <div className="composer-inline">
        <textarea
          value={composer}
          onChange={(event) => onComposerChange(event.target.value)}
          onKeyDown={handleKeyDown}
          onCompositionStart={handleCompositionStart}
          onCompositionEnd={handleCompositionEnd}
          placeholder="描述你的发明想法，或说明要补充的章节"
          rows={3}
          disabled={isBusy}
        />
        <div className="composer-toolbar">
          <div className="composer-toolbar-left">
            <div className="quality-mode-toggle" role="group" aria-label="生成模式">
              {(['normal', 'enhanced'] as const).map((mode) => (
                <button
                  key={mode}
                  type="button"
                  className={qualityMode === mode ? 'active' : ''}
                  onClick={() => onQualityModeChange(mode)}
                  disabled={isBusy}
                  aria-pressed={qualityMode === mode}
                >
                  {mode === 'normal' ? '普通模式' : '增强模式'}
                </button>
              ))}
            </div>
          </div>
          <div className="composer-toolbar-right">
            <ContextUsageBadge contextUsage={contextUsage} />
            <button
              className={`composer-send-inline ${canCancel ? 'cancel' : ''}`}
              type="button"
              onClick={canCancel ? onCancel : onSubmit}
              disabled={canCancel ? isCancelling : isBusy || composer.trim().length === 0}
              aria-label={canCancel ? '取消任务' : '发送'}
              title={canCancel ? '取消任务' : '发送'}
            >
              {canCancel ? <span className="composer-stop-icon" aria-hidden="true" /> : '↑'}
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}
