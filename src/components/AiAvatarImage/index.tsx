import { ImgHTMLAttributes } from 'react';
import DefaultAiAvatar from '@/assets/img/AiAvatar.svg';

function AiAvatarImage({ src, alt = 'AI 助手头像', ...rest }: ImgHTMLAttributes<HTMLImageElement>) {
  return (
    <img
      {...rest}
      src={src || DefaultAiAvatar}
      alt={alt}
      onError={(event) => {
        if (event.currentTarget.getAttribute('src') !== DefaultAiAvatar) {
          event.currentTarget.src = DefaultAiAvatar;
        }
      }}
    />
  );
}

export default AiAvatarImage;
