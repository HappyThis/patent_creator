import { useCallback, useState } from 'react';
import { initialSelectionState, updatedSelectionState } from '../mocks/demoRound';

export function useWorkspaceSelection() {
  const [activeSectionId, setActiveSectionId] = useState(initialSelectionState.activeSectionId);
  const [activeBlockId, setActiveBlockId] = useState<string | null>(
    initialSelectionState.activeBlockId,
  );
  const [recentSectionIds, setRecentSectionIds] = useState(initialSelectionState.recentSectionIds);
  const [recentBlockIds, setRecentBlockIds] = useState(initialSelectionState.recentBlockIds);

  const selectSection = useCallback((sectionId: string) => {
    setActiveSectionId(sectionId);
    setActiveBlockId(null);
  }, []);

  const focusUpdatedTechnicalSolution = useCallback(() => {
    setRecentSectionIds(updatedSelectionState.recentSectionIds);
    setRecentBlockIds(updatedSelectionState.recentBlockIds);
    setActiveSectionId(updatedSelectionState.activeSectionId);
    setActiveBlockId(updatedSelectionState.activeBlockId);
  }, []);

  return {
    activeSectionId,
    activeBlockId,
    recentSectionIds,
    recentBlockIds,
    setActiveSectionId,
    selectSection,
    focusUpdatedTechnicalSolution,
  };
}
