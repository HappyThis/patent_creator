import { useCallback, useState } from 'react';

export function useWorkspaceSelection() {
  const [activeSectionId, setActiveSectionId] = useState('');
  const [activeBlockId, setActiveBlockId] = useState<string | null>(null);
  const [recentSectionIds, setRecentSectionIds] = useState<string[]>([]);
  const [recentBlockIds, setRecentBlockIds] = useState<string[]>([]);

  const selectSection = useCallback((sectionId: string) => {
    setActiveSectionId(sectionId);
    setActiveBlockId(null);
  }, []);

  const syncActiveSection = useCallback((sectionId: string | null | undefined) => {
    if (sectionId) {
      setActiveSectionId(sectionId);
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
    setActiveSectionId,
    selectSection,
    syncActiveSection,
    focusDocumentChange,
    resetRecent,
  };
}
