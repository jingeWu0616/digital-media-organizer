"""Curated local looks. Applying a look never replaces the user's content."""
from pathlib import Path

ASSETS = Path(__file__).resolve().parent/'assets/watermark_examples'
LOOKS = {
    'corner': dict(name='极简签名', theme='dark', font='雅黑', size=3, opacity=90, position='bottom-right'),
    'frame': dict(name='经典白边', theme='light', font='雅黑', size=3, opacity=100, position='bottom-left'),
    'bar': dict(name='深色参数栏', theme='dark', font='雅黑', size=3, opacity=80, position='bottom-left'),
    'gallery': dict(name='留白画廊', theme='light', font='宋体', size=2.6, opacity=100, position='bottom-left'),
    'postcard': dict(name='旅行明信片', theme='light', font='雅黑', size=2.6, opacity=100, position='bottom-left'),
    'film': dict(name='胶片日期', theme='dark', font='黑体', size=2.8, opacity=95, position='bottom-right'),
    'memory': dict(name='家庭纪念', theme='light', font='宋体', size=2.6, opacity=100, position='bottom-left'),
    'outdoor': dict(name='户外记录', theme='dark', font='雅黑', size=2.6, opacity=90, position='bottom-left'),
}
EXAMPLES = (
    dict(id='landscape', observed_name='山景 · 横幅', filename='landscape.jpg', portrait=False,
         credit='Hannes Röst · CC BY-SA 3.0', link='https://commons.wikimedia.org/wiki/File:Fronalpstock_big.jpg'),
    dict(id='city', observed_name='城市 · 横幅', filename='city.jpg', portrait=False,
         credit='Allen McGregor · CC BY 2.0', link='https://commons.wikimedia.org/wiki/File:Chicago_skyline,_viewed_from_John_Hancock_Center.jpg'),
    dict(id='portrait', observed_name='城市 · 竖幅', filename='city.jpg', portrait=True,
         credit='Allen McGregor · CC BY 2.0 · 竖幅裁切', link='https://commons.wikimedia.org/wiki/File:Chicago_skyline,_viewed_from_John_Hancock_Center.jpg'),
)


def look_options(key):
    return dict(style=key, **{k:v for k,v in LOOKS[key].items() if k!='name'})


def example_rows():
    return [dict(item, current_path='', device='示例相机', capture_time='2026-11-09 09:04:32',
                 watermark_place='示例地点') for item in EXAMPLES]

