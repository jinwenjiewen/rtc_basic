/**
 * Copyright 2025 Beijing Volcano Engine Technology Co., Ltd. All Rights Reserved.
 * SPDX-license-identifier: BSD-3-Clause
 */

import { useEffect, useState, useRef } from 'react';
import { useSelector, useDispatch } from 'react-redux';
import VERTC, { MediaType } from '@volcengine/rtc';
import { Modal } from '@arco-design/web-react';
import RtcClient from '@/lib/RtcClient';
import {
  clearCurrentMsg,
  clearHistoryMsg,
  localJoinRoom,
  localLeaveRoom,
  updateAIGCState,
  updateLocalUser,
  updateRTCConfig,
  updateFullScreen,
  RTCConfig,
} from '@/store/slices/room';

import useRtcListeners from '@/lib/listenerHooks';
import { RootState } from '@/store';

import {
  updateMediaInputs,
  updateSelectedDevice,
  setDevicePermissions,
} from '@/store/slices/device';
import logger from '@/utils/logger';
import Apis from '@/app/index';

export const ABORT_VISIBILITY_CHANGE = 'abortVisibilityChange';
export interface FormProps {
  username: string;
  roomId: string;
  publishAudio: boolean;
}

export const useScene = () => {
  const { scene, sceneConfigMap } = useSelector((state: RootState) => state.room);
  return sceneConfigMap[scene] || {};
};

export const useRTC = () => {
  const { scene, rtcConfigMap } = useSelector((state: RootState) => state.room);
  return rtcConfigMap[scene] || {};
};

export const useDeviceState = () => {
  const dispatch = useDispatch();
  const room = useSelector((state: RootState) => state.room);
  const localUser = room.localUser;
  const isAudioPublished = localUser.publishAudio;
  const isVideoPublished = localUser.publishVideo;
  const isScreenPublished = localUser.publishScreen;
  const queryDevices = async (type: MediaType) => {
    const mediaDevices = await RtcClient.getDevices({
      audio: type === MediaType.AUDIO,
      video: type === MediaType.VIDEO,
    });
    if (type === MediaType.AUDIO) {
      dispatch(
        updateMediaInputs({
          audioInputs: mediaDevices.audioInputs,
        })
      );
      dispatch(
        updateSelectedDevice({
          selectedMicrophone: mediaDevices.audioInputs[0]?.deviceId,
        })
      );
    } else {
      dispatch(
        updateMediaInputs({
          videoInputs: mediaDevices.videoInputs,
        })
      );
      dispatch(
        updateSelectedDevice({
          selectedCamera: mediaDevices.videoInputs[0]?.deviceId,
        })
      );
    }
    return mediaDevices;
  };

  const switchMic = async (controlPublish = true) => {
    if (controlPublish) {
      await (!isAudioPublished
        ? RtcClient.publishStream(MediaType.AUDIO)
        : RtcClient.unpublishStream(MediaType.AUDIO));
    }
    queryDevices(MediaType.AUDIO);
    await (!isAudioPublished ? RtcClient.startAudioCapture() : RtcClient.stopAudioCapture());
    dispatch(
      updateLocalUser({
        publishAudio: !isAudioPublished,
      })
    );
  };

  const switchCamera = async (controlPublish = true) => {
    if (controlPublish) {
      await (!isVideoPublished
        ? RtcClient.publishStream(MediaType.VIDEO)
        : RtcClient.unpublishStream(MediaType.VIDEO));
    }
    queryDevices(MediaType.VIDEO);
    await (!isVideoPublished ? RtcClient.startVideoCapture() : RtcClient.stopVideoCapture());
    dispatch(
      updateLocalUser({
        publishVideo: !isVideoPublished,
      })
    );
  };

  const switchScreenCapture = async (controlPublish = true) => {
    try {
      !isScreenPublished
        ? sessionStorage.setItem(ABORT_VISIBILITY_CHANGE, 'true')
        : sessionStorage.removeItem(ABORT_VISIBILITY_CHANGE);
      if (controlPublish) {
        await (!isScreenPublished
          ? RtcClient.publishScreenStream(MediaType.VIDEO)
          : RtcClient.unpublishScreenStream(MediaType.VIDEO));
      }
      await (!isScreenPublished ? RtcClient.startScreenCapture() : RtcClient.stopScreenCapture());
      dispatch(
        updateLocalUser({
          publishScreen: !isScreenPublished,
        })
      );
    } catch {
      console.warn('Not Authorized.');
    }
    sessionStorage.removeItem(ABORT_VISIBILITY_CHANGE);
    return false;
  };

  return {
    isAudioPublished,
    isVideoPublished,
    isScreenPublished,
    switchMic,
    switchCamera,
    switchScreenCapture,
  };
};

export const useGetDevicePermission = () => {
  const [permission, setPermission] = useState<{
    audio: boolean;
  }>();

  const dispatch = useDispatch();

  useEffect(() => {
    (async () => {
      const permission = await RtcClient.checkPermission();
      dispatch(setDevicePermissions(permission));
      setPermission(permission);
    })();
  }, [dispatch]);
  return permission;
};

export const useJoin = (): [boolean, () => Promise<void | boolean>] => {
  const devicePermissions = useSelector((state: RootState) => state.device.devicePermissions);
  const room = useSelector((state: RootState) => state.room);

  const dispatch = useDispatch();

  const { id } = useScene();
  const { switchMic } = useDeviceState();
  const [joining, setJoining] = useState(false);
  const joiningRef = useRef(false);
  const listeners = useRtcListeners();

  const handleAIGCModeStart = async () => {
    if (room.isAIGCEnable) {
      await RtcClient.stopAgent(id);
      dispatch(clearCurrentMsg());
      await RtcClient.startAgent(id);
    } else {
      await RtcClient.startAgent(id);
    }
    dispatch(updateAIGCState({ isAIGCEnable: true }));
  };

  async function disPatchJoin(): Promise<boolean | undefined> {
    if (joiningRef.current) return;
    joiningRef.current = true;
    setJoining(true);
    try {
      const isSupported = await VERTC.isSupported();
      if (!isSupported) throw new Error('当前浏览器不支持 RTC，请升级浏览器后重试。');

      // Re-fetch credentials on each join instead of reusing a token from an old page load.
      const { scenes } = await Apis.Basic.getScenes();
      const configs = (scenes as { scene: { id: string }; rtc: RTCConfig }[]).reduce<
        Record<string, RTCConfig>
      >((all, scene) => ({ ...all, [scene.scene.id]: scene.rtc }), {});
      if (!configs[id]) throw new Error('未能获取当前场景的进房信息，请刷新页面重试。');
      dispatch(updateRTCConfig(configs));

      await RtcClient.createEngine();
      RtcClient.addEventListeners(listeners);
      await RtcClient.joinRoom();
      const mediaDevices = await RtcClient.getDevices({ audio: true, video: false });
      dispatch(
        updateSelectedDevice({
          selectedMicrophone: mediaDevices.audioInputs[0]?.deviceId,
          selectedCamera: mediaDevices.videoInputs[0]?.deviceId,
        })
      );
      dispatch(updateMediaInputs(mediaDevices));
      if (devicePermissions.audio) {
        try {
          await switchMic();
        } catch {
          logger.debug('No permission for mic');
        }
      }

      await handleAIGCModeStart();
      dispatch(
        localJoinRoom({
          roomId: RtcClient.basicInfo.room_id,
          user: { username: RtcClient.basicInfo.user_id, userId: RtcClient.basicInfo.user_id },
        })
      );
      return true;
    } catch (error) {
      if (RtcClient.getAgentEnabled()) await RtcClient.stopAgent(id).catch(() => {});
      await RtcClient.release().catch(() => {});
      dispatch(localLeaveRoom());
      dispatch(updateAIGCState({ isAIGCEnable: false }));
      dispatch(updateFullScreen({ isFullScreen: false }));
      const reason = error instanceof Error ? error.message : String(error);
      Modal.error({
        title: '连接失败',
        content: reason.includes('token_error')
          ? 'RTC 进房凭证校验失败，请核对 Token 的房间号、用户 ID 和有效期后重试。'
          : reason || '暂时无法连接，请稍后重试。',
      });
      return false;
    } finally {
      joiningRef.current = false;
      setJoining(false);
    }
  }

  return [joining, disPatchJoin];
};

export const useLeave = () => {
  const dispatch = useDispatch();
  const { id } = useScene();
  const idRef = useRef(id);
  idRef.current = id;

  return async function () {
    try {
      await RtcClient.stopAgent(idRef.current);
    } catch (error) {
      console.warn('停止云端语音任务失败', error);
    }
    await RtcClient.release();
    dispatch(clearHistoryMsg());
    dispatch(clearCurrentMsg());
    dispatch(localLeaveRoom());
    dispatch(updateAIGCState({ isAIGCEnable: false }));
  };
};
