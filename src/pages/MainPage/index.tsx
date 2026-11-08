/**
 * Copyright 2025 Beijing Volcano Engine Technology Co., Ltd. All Rights Reserved.
 * SPDX-license-identifier: BSD-3-Clause
 */

import { useEffect, useRef, useState } from 'react';
import { useDispatch } from 'react-redux';
import { Button, Spin } from '@arco-design/web-react';
import Header from '@/components/Header';
import ResizeWrapper from '@/components/ResizeWrapper';
import Menu from './Menu';
import { useIsMobile } from '@/utils/utils';
import Apis from '@/app/index';
import MainArea from './MainArea';
import { ABORT_VISIBILITY_CHANGE, useLeave } from '@/lib/useCommon';
import {
  RTCConfig,
  SceneConfig,
  resetRoom,
  updateRTCConfig,
  updateScene,
  updateSceneConfig,
} from '@/store/slices/room';
import RtcClient from '@/lib/RtcClient';
import styles from './index.module.less';

export default function () {
  const leaveRoom = useLeave();
  const dispatch = useDispatch();
  const [ready, setReady] = useState(false);
  const [loadError, setLoadError] = useState('');
  const mounted = useRef(true);

  const getScenes = async () => {
    setLoadError('');
    try {
      const {
        scenes,
      }: {
        scenes: {
          rtc: RTCConfig;
          scene: SceneConfig;
        }[];
      } = await Apis.Basic.getScenes();
      if (!mounted.current) return;
      dispatch(updateScene(scenes[0].scene.id));
      dispatch(
        updateSceneConfig(
          scenes.reduce<Record<string, SceneConfig>>((prev, cur) => {
            prev[cur.scene.id] = cur.scene;
            return prev;
          }, {})
        )
      );
      dispatch(
        updateRTCConfig(
          scenes.reduce<Record<string, RTCConfig>>((prev, cur) => {
            prev[cur.scene.id] = cur.rtc;
            return prev;
          }, {})
        )
      );
      setReady(true);
    } catch (error) {
      if (mounted.current) {
        setLoadError(error instanceof Error ? error.message : '无法加载对话场景。');
      }
    }
  };

  useEffect(() => {
    mounted.current = true;
    getScenes();
    const isOriginalDemo = window.location.host.startsWith('localhost');
    const handler = () => {
      if (
        document.visibilityState === 'hidden' &&
        !sessionStorage.getItem(ABORT_VISIBILITY_CHANGE)
      ) {
        leaveRoom();
      }
    };
    !isOriginalDemo && document.addEventListener('visibilitychange', handler);
    return () => {
      mounted.current = false;
      !isOriginalDemo && document.removeEventListener('visibilitychange', handler);
      RtcClient.release().catch(console.warn);
      sessionStorage.removeItem('audioBotEnabled');
      sessionStorage.removeItem('RequestID');
      sessionStorage.removeItem(ABORT_VISIBILITY_CHANGE);
      dispatch(resetRoom());
    };
  }, []);

  return (
    <ResizeWrapper className={styles.container}>
      <Header />
      <div
        className={styles.main}
        style={{
          padding: useIsMobile() ? '' : '24px',
        }}
      >
        <div className={`${styles.mainArea} ${useIsMobile() ? styles.isMobile : ''}`}>
          {ready ? (
            <MainArea />
          ) : (
            <div className={styles.sceneStatus} role="status">
              {loadError ? (
                <>
                  <p>{loadError}</p>
                  <Button onClick={getScenes}>重试</Button>
                </>
              ) : (
                <Spin tip="正在准备对话空间" />
              )}
            </div>
          )}
        </div>
        {useIsMobile() || !ready ? null : (
          <div className={styles.operationArea}>
            <Menu />
          </div>
        )}
      </div>
    </ResizeWrapper>
  );
}
