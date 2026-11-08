/**
 * Copyright 2025 Beijing Volcano Engine Technology Co., Ltd. All Rights Reserved.
 * SPDX-license-identifier: BSD-3-Clause
 */

import { useSelector } from 'react-redux';
import Conversation from './Conversation';
import ToolBar from './ToolBar';
import CameraArea from './CameraArea';
import AudioController from './AudioController';
import TextComposer from './TextComposer';
import { isMobile } from '@/utils/utils';
import style from './index.module.less';
import AiAvatarCard from '@/components/AiAvatarCard';
import { RootState } from '@/store';
import UserTag from '@/components/UserTag';
import FullScreenCard from '@/components/FullScreenCard';
import MobileToolBar from '@/pages/Mobile/MobileToolBar';
import { useScene } from '@/lib/useCommon';

function Room() {
  const room = useSelector((state: RootState) => state.room);
  const { isShowSubtitle, scene, isFullScreen } = room;
  const { isAvatarScene, isVision, isScreenMode } = useScene();
  const hasCameraPreview =
    isFullScreen ||
    isAvatarScene ||
    isVision ||
    isScreenMode ||
    room.localUser.publishVideo ||
    room.localUser.publishScreen;
  return (
    <div className={`${style.wrapper} ${isMobile() ? style.mobile : ''}`}>
      {isMobile() ? <div className={style.mobilePlayer} id="mobile-local-player" /> : null}
      {isMobile() ? <MobileToolBar /> : null}
      {isShowSubtitle && !isMobile() ? (
        <UserTag name={scene} className={style.subTitleUserTag} />
      ) : null}
      {(isFullScreen || isAvatarScene) && !isMobile() ? (
        <FullScreenCard />
      ) : isMobile() && isShowSubtitle ? null : (
        <AiAvatarCard
          showUserTag={!isShowSubtitle}
          showStatus={!isShowSubtitle}
          className={isShowSubtitle ? style.subtitleAiAvatar : ''}
        />
      )}
      {isMobile() ? null : (
        <CameraArea
          className={isShowSubtitle && !hasCameraPreview ? style.hiddenCameraPreview : ''}
        />
      )}
      <Conversation
        className={`${style.conversation} ${hasCameraPreview ? style.withCameraPreview : ''}`}
        showSubtitle={isShowSubtitle}
      />
      <TextComposer />
      <ToolBar className={style.toolBar} />
      <AudioController className={style.controller} />
      <div className={style.declare}>AI生成内容由大模型生成，不能完全保障真实</div>
    </div>
  );
}

export default Room;
