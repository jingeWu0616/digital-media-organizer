"""Additional proportional photo layouts; originals are never cropped or resized."""
import math
from PIL import Image, ImageDraw, ImageFont

EXTENDED_STYLES = {'gallery', 'postcard', 'film', 'memory', 'outdoor'}


def render_layout(image, text, options, logo, font_file):
    width, height = image.size
    short = min(width, height)
    style, light = options['style'], options['theme']=='light'
    pad = max(2, round(short*.035))
    base = max(1, round(short*options['size']/100))
    body_font = ImageFont.truetype(str(font_file), base)
    title_font = ImageFont.truetype(str(font_file), max(1, round(base*1.5)))
    alpha = round(options['opacity']*255/100)
    brand = logo.copy() if logo else None
    try:
        if brand:
            ratio = short*options['logo_size']/100/max(brand.size)
            resized = brand.resize((max(1,round(brand.width*ratio)),max(1,round(brand.height*ratio))),Image.Resampling.LANCZOS)
            brand.close(); brand=resized
            if alpha<255: brand.putalpha(brand.getchannel('A').point(lambda a:round(a*alpha/255)))
        framed = style in ('gallery','postcard','memory')
        margin = round(short*({'gallery':.045,'postcard':.025,'memory':.035}.get(style,0)))
        available = width - 2*pad - (brand.width+pad if brand else 0)
        if available<=0: raise ValueError('照片尺寸过小，无法放置水印。')
        probe=ImageDraw.Draw(Image.new('RGB',(1,1)))
        rows=[]
        for number, paragraph in enumerate(text.splitlines()):
            font = title_font if number==0 and options.get('title') else body_font
            line=''
            for char in paragraph:
                if line and probe.textlength(line+char,font=font)>available:
                    rows.append((line,font));line=''
                line+=char
            if line: rows.append((line,font))
        gap=max(1,round(base*.4))
        line_heights=[sum(font.getmetrics())+gap for _,font in rows]
        text_height=sum(line_heights)-(gap if rows else 0)
        text_width=math.ceil(max((probe.textlength(line,font=font) for line,font in rows),default=0))
        content_height=max(text_height,brand.height if brand else 0)
        if content_height+2*pad>height*.55: raise ValueError('水印内容过多，请减少文字或调小字号。')
        bg = (250,247,240) if style in ('memory','postcard') and light else (255,255,255) if light else (23,29,36)
        fg = (48,46,42) if light else (245,247,250)
        muted = (106,103,96) if light else (190,201,210)
        if style=='film':
            fg=(137,70,20) if light else (255,194,105)
            muted=fg
        footer_height=max(content_height+2*pad, round(short*.14))
        patch_width=width+2*margin if framed else min(width,text_width+2*pad+(brand.width+pad if brand else 0))
        patch=Image.new('RGBA',(patch_width,footer_height),(0,0,0,0))
        try:
            draw=ImageDraw.Draw(patch)
            if framed: draw.rectangle((0,0,patch_width,footer_height),fill=(*bg,255))
            elif style=='outdoor':
                draw.rounded_rectangle((0,0,patch_width-1,footer_height-1),radius=max(1,pad//3),fill=(*bg,round(alpha*.8)))
                draw.rectangle((0,pad,max(1,round(short*.004)),footer_height-pad),fill=(208,158,79,alpha))
            centered=style in ('gallery','memory')
            group_width=text_width+(brand.width+pad if brand else 0)
            x=(patch_width-group_width)//2 if centered else pad+margin
            if brand:
                patch.alpha_composite(brand,(x,(footer_height-brand.height)//2));x+=brand.width+pad
            y=(footer_height-text_height)//2
            for n,((line,font),line_height) in enumerate(zip(rows,line_heights)):
                line_x=x+(text_width-round(draw.textlength(line,font=font)))//2 if centered else x
                color=fg if n==0 or style=='film' else muted
                if style=='film':
                    draw.text((line_x,y),line,font=font,fill=(*color,alpha),anchor='lt',
                              stroke_width=max(1,round(base*.035)),stroke_fill=(0,0,0,round(alpha*.65)))
                else: draw.text((line_x,y),line,font=font,fill=(*color,alpha),anchor='lt')
                y+=line_height
            if style=='postcard':
                draw.line((pad+margin,0,patch_width-pad-margin,0),fill=(*muted,60),width=max(1,round(short*.001)))
            if framed:
                output=Image.new('RGB',(width+2*margin,height+margin+footer_height),bg)
                output.paste(image,(margin,margin))
                output.paste(patch,(0,height+margin),patch)
            else:
                output=image.copy()
                x=width-patch_width-pad if options['position'].endswith('right') else pad
                y=pad if options['position'].startswith('top') else height-footer_height-pad
                output.paste(patch,(max(0,x),max(0,y)),patch)
            return output
        finally: patch.close()
    finally:
        if brand: brand.close()

