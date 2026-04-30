import { useRef } from 'react';
import { ChatPanel } from '../features/chat/ChatPanel';
import { OutlinePanel } from '../features/outline/OutlinePanel';
import { buildDocumentStats } from '../features/preview/documentStats';
import { PreviewPanel } from '../features/preview/PreviewPanel';
import { useWorkspace } from '../hooks/useWorkspace';

function App() {
  const previewRef = useRef<HTMLDivElement | null>(null);
  const {
    renderAst,
    events,
    composer,
    isBusy,
    contextUsage,
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
    handleNewSession,
  } = useWorkspace();
  const documentStats = buildDocumentStats(renderAst);

  return (
    <div className="app-shell">
      <main className="workspace">
        <section className="document-workspace">
          <OutlinePanel
            outline={renderAst.outline}
            activeSectionId={activeSectionId}
            recentSectionIds={recentSectionIds}
            sectionStatusById={documentStats.sectionStatusById}
            onSelect={selectSection}
          />

          <PreviewPanel
            renderAst={renderAst}
            previewFocusTarget={previewFocusTarget}
            recentSectionIds={recentSectionIds}
            recentBlockIds={recentBlockIds}
            stats={documentStats}
            previewRef={previewRef}
            onActiveSectionChange={setActiveSectionId}
          />
        </section>

        <ChatPanel
          sessionTabs={sessionTabs}
          events={events}
          composer={composer}
          isBusy={isBusy}
          contextUsage={contextUsage}
          onComposerChange={setComposer}
          onSubmit={submitMessage}
          onSessionSelect={handleSessionSelect}
          onNewSession={handleNewSession}
        />
      </main>
    </div>
  );
}

export default App;
