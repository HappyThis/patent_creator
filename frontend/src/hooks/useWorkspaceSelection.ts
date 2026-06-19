import { useCallback, useState } from 'react';

export type PreviewFocusTarget = {
  sectionId: string;
  blockId: string | null;
  nonce: number;
};

export function useWorkspaceSelection() {
  const [activeSectionId, setActiveSectionId] = useState('');
  const [activeBlockId, setActiveBlockId] = useState<string | null>(null);
  const [recentSectionIds, setRecentSectionIds] = useState<string[]>([]);
  const [recentBlockIds, setRecentBlockIds] = useState<string[]>([]);
  const [previewFocusTarget, setPreviewFocusTarget] = useState<PreviewFocusTarget | null>(null);

  const syncActiveSection = useCallback((sectionId: string | null | undefined) => {
    setActiveSectionId(sectionId || '');
    if (!sectionId) {
      setActiveBlockId(null);
    }
  }, []);

  const focusDocumentChange = useCallback(
    (payload: {
      active_section_id?: string | null;
      active_block_id?: string | null;
      changed_section_ids?: string[];
      changed_block_ids?: string[];
    }) => {
      setRecentSectionIds(payload.changed_section_ids ?? []);
      setRecentBlockIds(payload.changed_block_ids ?? []);
      if (payload.active_section_id) {
        setActiveSectionId(payload.active_section_id);
        setPreviewFocusTarget({
          sectionId: payload.active_section_id,
          blockId: payload.active_block_id ?? null,
          nonce: Date.now(),
        });
      }
      setActiveBlockId(payload.active_block_id ?? null);
    },
    [],
  );

  const resetRecent = useCallback(() => {
    setRecentSectionIds([]);
    setRecentBlockIds([]);
  }, []);

  return {
    activeSectionId,
    activeBlockId,
    recentSectionIds,
    recentBlockIds,
    previewFocusTarget,
    setActiveSectionId,
    syncActiveSection,
    focusDocumentChange,
    resetRecent,
  };
}
