import { useRef } from 'react';
import { ChatPanel } from '../features/chat/ChatPanel';
import { OutlinePanel } from '../features/outline/OutlinePanel';
import { PreviewPanel } from '../features/preview/PreviewPanel';
import { useWorkspace } from '../hooks/useWorkspace';

function App() {
  const previewRef = useRef<HTMLDivElement | null>(null);
  const {
    renderAst,
    events,
    composer,
    isBusy,
    sessionTabs,
    activeSectionId,
    previewFocusTarget,
    recentSectionIds,
    recentBlockIds,
    setComposer,
    setActiveSectionId,
    selectSection,
    submitMessage,
    handleSessionSelect,
  } = useWorkspace();

  return (
    <div className="app-shell">
      <main className="workspace">
        <section className="document-workspace">
          <OutlinePanel
            outline={renderAst.outline}
            activeSectionId={activeSectionId}
            recentSectionIds={recentSectionIds}
            onSelect={selectSection}
          />

          <PreviewPanel
            renderAst={renderAst}
            previewFocusTarget={previewFocusTarget}
            recentSectionIds={recentSectionIds}
            recentBlockIds={recentBlockIds}
            previewRef={previewRef}
            onActiveSectionChange={setActiveSectionId}
          />
        </section>

        <ChatPanel
          sessionTabs={sessionTabs}
          events={events}
          composer={composer}
          isBusy={isBusy}
          onComposerChange={setComposer}
          onSubmit={submitMessage}
          onSessionSelect={handleSessionSelect}
        />
      </main>
    </div>
  );
}

export default App;
