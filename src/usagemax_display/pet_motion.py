"""Native-frame playback of generated sprites and smooth habitat navigation."""
from __future__ import annotations

import json
import math
from dataclasses import dataclass
from pathlib import Path

from PIL import Image


@dataclass(frozen=True)
class MotionClip:
    frames: tuple[Image.Image, ...]
    fps: float
    loop: bool = True
    anchors_x: tuple[float, ...] = ()

    def index(self, elapsed: float) -> int:
        tick = max(0, math.floor(max(0.0, elapsed) * self.fps + 1e-7))
        return tick % len(self.frames) if self.loop else min(tick, len(self.frames)-1)


class MotionLibrary:
    """Decode generated poses once and present one crisp pose per frame."""
    def __init__(self, directory: Path, size=(190, 206)):
        self.clips: dict[str, MotionClip] = {}
        self.errors: list[str] = []
        self.scaled = {}
        manifest = directory / 'manifest.json'
        if not manifest.is_file():
            return
        try:
            data = json.loads(manifest.read_text())
            if not isinstance(data, dict) or data.get('version') != 1:
                raise ValueError('unsupported animation manifest')
            if not isinstance(data.get('clips'), dict) or not data['clips']:
                raise ValueError('missing animation clips')
            for name, spec in data['clips'].items():
                path = (directory / spec['sheet']).resolve()
                if not path.is_relative_to(directory.resolve()):
                    raise ValueError('animation sheet escapes asset directory')
                columns, rows = int(spec['columns']), int(spec['rows'])
                count, fps = int(spec['frames']), float(spec['fps'])
                cell_w, cell_h = map(int, spec['cell'])
                if not (1 <= count <= 120 and count <= columns*rows and 1 <= fps <= 60):
                    raise ValueError('invalid animation timing or geometry')
                anchors = tuple(float(value) for value in spec.get('anchors_x', ()))
                if anchors and (len(anchors) != count or any(not math.isfinite(x) or not 0 <= x <= 1 for x in anchors)):
                    raise ValueError(f'invalid frame anchors for {name}')
                with Image.open(path) as source:
                    if source.size != (columns*cell_w, rows*cell_h):
                        raise ValueError(f'wrong sheet size for {name}')
                    sheet = source.convert('RGBA')
                frames = []
                for index in range(count):
                    x, y = index % columns * cell_w, index // columns * cell_h
                    frame = sheet.crop((x,y,x+cell_w,y+cell_h))
                    if frame.getchannel('A').getbbox() is None:
                        raise ValueError(f'empty frame {name}:{index}')
                    frames.append(frame.resize(size, Image.Resampling.LANCZOS))
                self.clips[name] = MotionClip(tuple(frames), fps, bool(spec.get('loop', True)), anchors)
        except (OSError, ValueError, KeyError, TypeError, ZeroDivisionError) as error:
            self.errors.append(str(error))
            self.clips.clear()

    @property
    def ready(self):
        return bool(self.clips)

    def frame(self, state: str, elapsed: float):
        clip = self.clips.get(state) or self.clips.get('idle')
        return clip.frames[clip.index(elapsed)] if clip else None

    def presentation(self, pose, now):
        """Present a single generated pose; blending silhouettes causes ghosting."""
        clip = self.clips.get(pose.state) or self.clips.get('idle')
        if clip is None:
            return None
        key = (pose.state, pose.scale)
        if key not in self.scaled:
            self.scaled[key] = tuple(frame.resize((round(frame.width*pose.scale), round(frame.height*pose.scale)),
                Image.Resampling.LANCZOS) for frame in clip.frames)
        return self.scaled[key][clip.index(pose.elapsed)]

    def anchor_x(self, state, elapsed):
        """Anchor the body, not the contact sheet's uneven cell placement."""
        clip = self.clips.get(state) or self.clips.get('idle')
        return clip.anchors_x[clip.index(elapsed)] if clip and clip.anchors_x else .5


@dataclass(frozen=True)
class HabitatPose:
    state: str
    elapsed: float
    x: float
    foot_y: float
    scale: float
    label: str

    def placement(self, habitat, sprite_size, anchor_x=.5):
        """Contain the entire sprite above the habitat's caption strip."""
        left,top,right,bottom=habitat
        width,height=sprite_size
        foot_x=left+self.x*(right-left)
        foot_y=top+self.foot_y*(bottom-top-16)
        return (round(max(left+12,min(right-12-width,foot_x-width*anchor_x))),
                round(max(top+12,min(bottom-37-height,foot_y-height*.923))))


class HabitatDirector:
    """A small companion: long rests, occasional gestures and short strolls.

    Screen refresh and action tempo are independent. ``elapsed`` returned to the
    atlas is normalized clip time, so 30 drawn poses can take several seconds.
    """
    ACTIONS = (
        ('waiting', 'Watching the world'), ('groom', 'Fixing her hair'),
        ('review', 'A curious little look'), ('stretch', 'A little stretch'),
        ('uwu', 'Feeling cozy'), ('snack', 'A little snack'),
        ('wink', 'Just for you'), ('waving', 'Saying hello'),
        ('kiss', 'Sending a kiss'), ('investigate', 'What is that?'),
        ('dance', 'A little happy dance'), ('jumping', 'Happy hop'),
    )
    REACTIONS = {
        'greeting': ('waving', 'Hello again'),
        'victory': ('dance', 'Celebrating the win'),
        'snack-dance': ('snack', 'Token snack'),
        'mega-feast': ('snack', 'A well-earned treat'),
        'cooldown': ('failed', 'Shaking it off'),
    }
    TEMPO = {'idle':6.0,'nap':8.0,'waiting':4.8,'review':4.5,'groom':5.0,
             'stretch':5.5,'uwu':4.2,'snack':5.0,'wink':3.6,'waving':3.6,
             'kiss':4.4,'investigate':4.8,'dance':4.5,'jumping':1.8,
             'failed':4.0,'running':3.0,'running-left':2.6,'running-right':2.6}
    # A stroll is at most 47 pixels, on one ground plane. No perspective zoom.
    SPOTS = (.41,.49,.60,.51,.62,.54,.44,.50)
    FOOT_Y = .86
    SCALE = .82

    def __init__(self):
        self.started = False
        self.x, self.y = .5, self.FOOT_Y
        self.origin = (self.x,self.y)
        self.destination = self.origin
        self.walking = False
        self.phase_at = 0.0
        self.until = 0.0
        self.clip = 'idle'
        self.label = 'Taking it in'
        self.action_index = -1
        self.spot_index = 0
        self.reaction_key = None
        self.pending_reaction = None
        self.phase = 'rest'
        self.actions_since_nap = 0
        self.next_reaction = 0.0
        self.previous_now = None

    @property
    def resting(self):
        return self.phase == 'nap'

    def _position(self, now):
        if not self.walking:
            return self.x, self.y
        t = max(0.0,min(1.0,(now-self.phase_at)/max(.001,self.until-self.phase_at)))
        eased = t*t*(3-2*t)
        return (self.origin[0]+(self.destination[0]-self.origin[0])*eased,
                self.origin[1]+(self.destination[1]-self.origin[1])*eased)

    def _begin(self, phase, clip, label, now, duration):
        self.phase,self.clip,self.label=phase,clip,label
        self.phase_at,self.until=now,now+duration
        self.walking=phase=='walk'

    def _action(self, now):
        if self.pending_reaction and self.pending_reaction[2] >= now and now >= self.next_reaction:
            clip,label,_=self.pending_reaction
            self.next_reaction=now+60
        else:
            self.action_index=(self.action_index+1)%len(self.ACTIONS)
            clip,label=self.ACTIONS[self.action_index]
        self.pending_reaction=None
        self._begin('action',clip,label,now,self.TEMPO[clip]+.65)

    def _stroll_to(self, destination, now):
        self.origin = (self.x,self.y)
        self.destination = (destination,self.FOOT_Y)
        dx = (destination-self.x)*420
        clip='running-right' if dx>=0 else 'running-left'
        duration=max(2.6,math.ceil(abs(dx)/14/2.6)*2.6)
        self._begin('walk',clip,'A tiny stroll',now,duration)

    def sample(self, now, routine='', routine_started=0.0, scene='', enabled=True, energy=70.0):
        if not enabled:
            return HabitatPose('idle',0,self.x,self.FOOT_Y,self.SCALE,'Taking it easy')
        if not self.started:
            self.started = True
            self._begin('rest','idle','Taking it easy',now,4 if energy<25 else 12)
        if self.previous_now is not None and now-self.previous_now > 2:
            # Resume after a suspended renderer at the same position/pose.
            pause=now-self.previous_now
            self.phase_at+=pause
            self.until+=pause
        self.previous_now=now
        reaction_key = (routine,routine_started)
        if reaction_key != self.reaction_key:
            self.reaction_key = reaction_key
            if routine in self.REACTIONS and now >= self.next_reaction:
                self.pending_reaction = (*self.REACTIONS[routine],now+20)
        self.x,self.y = self._position(now)
        if now >= self.until:
            if self.phase=='walk':
                self.x,self.y = self.destination
                self._begin('arrive','idle','Found a cozy spot',now,1.8)
            elif self.phase=='arrive':
                self._action(now)
            elif self.phase=='action':
                self.actions_since_nap+=1
                self._begin('rest','idle','Taking it easy',now,18+(self.action_index%3)*4)
            elif self.phase=='settle':
                self._begin('nap','nap','A cozy catnap',now,40 if energy<25 else 28)
                self.actions_since_nap=0
                self.pending_reaction=None
            elif self.phase=='nap':
                self._begin('wake','investigate','Waking up slowly',now,1.8)
            elif self.phase=='wake':
                self._begin('rest','idle','Just waking up',now,10)
            elif energy<25 or self.actions_since_nap>=2 or routine=='catnap':
                self._begin('settle','investigate','Settling down',now,1.8)
            elif self.actions_since_nap==1:
                destination = self.SPOTS[self.spot_index % len(self.SPOTS)]
                self.spot_index += 1
                self._stroll_to(destination,now)
            else:
                self._action(now)
        elapsed=max(0,now-self.phase_at)
        if self.walking:
            # Gait phase follows distance travelled through the same easing
            # curve. Feet slow down with the body at departure and arrival.
            t=max(0,min(1,elapsed/(self.until-self.phase_at)))
            clock=t*t*(3-2*t)*(self.until-self.phase_at)/self.TEMPO[self.clip]
        elif self.phase=='settle':
            clock=min(.44,elapsed/1.8*.44)
        elif self.phase=='wake':
            clock=max(0,.44-elapsed/1.8*.44)
        else:
            clock=elapsed/self.TEMPO.get(self.clip,4.0)
            if self.phase=='action':
                clock=min(29/30,clock)
        return HabitatPose(self.clip,clock,self.x,self.y,self.SCALE,self.label)
