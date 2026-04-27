import { useCallback, useMemo, useState } from 'react';
import { initialEvents, initialProject, initialRenderAst } from '../mocks/demoData';
import {
  applyTechnicalSolutionReplacement,
  buildSessionTabs,
  createRoundCompletionEvent,
  createRoundProcessSteps,
  createRoundStartEvents,
} from '../mocks/demoRound';
import { ChatEvent } from '../types';
import { useWorkspaceSelection } from './useWorkspaceSelection';

const delay = (ms: number) => new Promise((resolve) => window.setTimeout(resolve, ms));

export function useDemoWorkspace() {
  const [project, setProject] = useState(initialProject);
  const [renderAst, setRenderAst] = useState(initialRenderAst);
  const [events, setEvents] = useState(initialEvents);
  const [composer, setComposer] = useState('请补充技术方案中的实时性约束与处理流程。');
  const {
    activeSectionId,
    activeBlockId,
    recentSectionIds,
    recentBlockIds,
    setActiveSectionId,
    selectSection,
    focusUpdatedTechnicalSolution,
  } = useWorkspaceSelection();

  const isBusy = project.isBusy;

  const sessionTabs = useMemo(() => buildSessionTabs(project), [project]);

  const appendEvent = useCallback((event: ChatEvent) => {
    setEvents((current) => [...current, event]);
  }, []);

  const simulateRound = useCallback(async () => {
    const trimmed = composer.trim();
    if (!trimmed || isBusy) {
      return;
    }

    setProject((current) => ({
      ...current,
      isBusy: true,
      runningRoundId: 'round_demo_001',
      activeSessionId: 'sess_004',
    }));
    setEvents((current) => [...current, ...createRoundStartEvents(trimmed)]);

    for (const step of createRoundProcessSteps()) {
      await delay(step.delayMs);
      appendEvent(step.event);
    }

    setRenderAst((current) => applyTechnicalSolutionReplacement(current));
    focusUpdatedTechnicalSolution();

    await delay(600);
    appendEvent(createRoundCompletionEvent());

    setProject((current) => ({
      ...current,
      isBusy: false,
      runningRoundId: null,
    }));
  }, [appendEvent, composer, focusUpdatedTechnicalSolution, isBusy]);

  return {
    renderAst,
    events,
    composer,
    isBusy,
    sessionTabs,
    activeSectionId,
    activeBlockId,
    recentSectionIds,
    recentBlockIds,
    setComposer,
    setActiveSectionId,
    selectSection,
    simulateRound,
  };
}
