
from panda3d.core import Texture, Camera, Vec3, Vec2, NodePath, RenderState
from panda3d.core import CullFaceAttrib, ColorWriteAttrib, DepthWriteAttrib
from panda3d.core import OmniBoundingVolume, PTAInt

from Light import Light
from DebugObject import DebugObject
from BetterShader import BetterShader
from RenderTarget import RenderTarget
from ShadowSource import ShadowSource
from ShadowAtlas import ShadowAtlas
from ShaderStructArray import ShaderStructArray
from Globals import Globals

from panda3d.core import PStatCollector

pstats_ProcessLights = PStatCollector("App:LightManager:ProcessLights")
pstats_CullLights = PStatCollector("App:LightManager:CullLights")
pstats_PerLightUpdates = PStatCollector("App:LightManager:PerLightUpdates")
pstats_FetchShadowUpdates = PStatCollector(
    "App:LightManager:FetchShadowUpdates")


class LightManager(DebugObject):

    """ This class is internally used by the RenderingPipeline to handle
    Lights and their Shadows. It stores a list of lights, and updates the
    required ShadowSources per frame. There are two main update methods:

    updateLights processes each light and does a basic frustum check.
    If the light is in the frustum, its ID is passed to the light precompute
    container (set with setLightingCuller). Also, each shadowSource of
    the light is checked, and if it reports to be invalid, it's queued to
    the list of queued shadow updates.

    updateShadows processes the queued shadow updates and setups everything
    to render the shadow depth textures to the shadow atlas.

    Lights can be added with addLight. Notice you cannot change the shadow
    resolution or even wether the light casts shadows after you called addLight.
    This is because it might already have a position in the atlas, and so
    the atlas would have to delete it's map, which is not supported (yet).
    This shouldn't be an issue, as you usually always know before if a
    light will cast shadows or not.

    """

    def __init__(self, pipeline):
        """ Creates a new LightManager. It expects a RenderPipeline as parameter. """
        DebugObject.__init__(self, "LightManager")

        self._initArrays()

        self.pipeline = pipeline
        self.settings = pipeline.getSettings()

        # Create arrays to store lights & shadow sources
        self.lights = []
        self.shadowSources = []
        self.queuedShadowUpdates = []
        self.allLightsArray = ShaderStructArray(Light, self.maxTotalLights)

        self.cullBounds = None
        self.shadowScene = Globals.render

        # Create atlas
        self.shadowAtlas = ShadowAtlas()
        self.shadowAtlas.setSize(self.settings.shadowAtlasSize)
        self.shadowAtlas.create()

        self.maxShadowMaps = 24
        self.maxShadowUpdatesPerFrame = self.settings.maxShadowUpdatesPerFrame
        self.numShadowUpdatesPTA = PTAInt.emptyArray(1)

        self.updateShadowsArray = ShaderStructArray(
            ShadowSource, self.maxShadowUpdatesPerFrame)
        self.allShadowsArray = ShaderStructArray(
            ShadowSource, self.maxShadowMaps)

        # Create shadow compute buffer
        self._createShadowComputationBuffer()

        # Create the initial shadow state
        self.shadowComputeCamera.setTagStateKey("ShadowPassShader")
        self.shadowComputeCamera.setInitialState(RenderState.make(
            ColorWriteAttrib.make(ColorWriteAttrib.C_off),
            DepthWriteAttrib.make(DepthWriteAttrib.M_on),
            # CullFaceAttrib.make(CullFaceAttrib.MCullNone),
            100))

        self._createTagStates()

        self.shadowScene.setTag("ShadowPassShader", "Default")

        # Create debug overlay
        self._createDebugTexts()

        # Disable buffer on start
        self.shadowComputeTarget.setActive(False)

        # Bind arrays
        self.updateShadowsArray.bindTo(self.shadowScene, "updateSources")
        self.updateShadowsArray.bindTo(
            self.shadowComputeTarget, "updateSources")

        # Set initial inputs
        for target in [self.shadowComputeTarget, self.shadowScene]:
            target.setShaderInput("numUpdates", self.numShadowUpdatesPTA)

        self.lightingComputator = None
        self.lightCuller = None

    def _createTagStates(self):
        # Create shadow caster shader
        self.shadowCasterShader = BetterShader.load(
            "Shader/DefaultShadowCaster/vertex.glsl",
            "Shader/DefaultShadowCaster/fragment.glsl",
            "Shader/DefaultShadowCaster/geometry.glsl")

        initialState = NodePath("ShadowCasterState")
        initialState.setShader(self.shadowCasterShader, 30)
        self.shadowComputeCamera.setTagState(
            "Default", initialState.getState())

    def _createShadowComputationBuffer(self):
        """ This creates the internal shadow buffer which also is the
        shadow atlas. Shadow maps are rendered to this using Viewports
        (thank you rdb for adding this!). It also setups the base camera
        which renders the shadow objects, although a custom mvp is passed
        to the shaders, so the camera is mainly a dummy """

        # Create camera showing the whole scene
        self.shadowComputeCamera = Camera("ShadowComputeCamera")
        self.shadowComputeCameraNode = self.shadowScene.attachNewNode(
            self.shadowComputeCamera)
        self.shadowComputeCamera.getLens().setFov(90, 90)
        self.shadowComputeCamera.getLens().setNearFar(10.0, 100000.0)

        # Disable culling
        self.shadowComputeCamera.setBounds(OmniBoundingVolume())

        self.shadowComputeCameraNode.setPos(0, 0, 150)
        self.shadowComputeCameraNode.lookAt(0, 0, 0)

        self.shadowComputeTarget = RenderTarget("ShadowCompute")
        self.shadowComputeTarget.setSize(self.shadowAtlas.getSize())
        self.shadowComputeTarget.addDepthTexture()
        self.shadowComputeTarget.setDepthBits(32)
        self.shadowComputeTarget.setSource(
            self.shadowComputeCameraNode, Globals.base.win)
        self.shadowComputeTarget.prepareSceneRender()

        # We have to adjust the sort
        self.shadowComputeTarget.getInternalRegion().setSort(3)
        self.shadowComputeTarget.getRegion().setSort(3)

        self.shadowComputeTarget.getInternalRegion().setNumRegions(
            self.maxShadowUpdatesPerFrame + 1)

        # The first viewport always has to be fullscreen
        self.shadowComputeTarget.getInternalRegion().setDimensions(
            0, (0, 1, 0, 1))
        self.shadowComputeTarget.setClearDepth(False)

        # We can't clear the depth per viewport.
        # But we need to clear it in any way, as we still want
        # z-testing in the buffers. So well, we create a
        # display region *below* (smaller sort value) each viewport
        # which has a depth-clear assigned. This is hacky, I know.
        self.depthClearer = []

        for i in range(self.maxShadowUpdatesPerFrame):
            buff = self.shadowComputeTarget.getInternalBuffer()
            dr = buff.makeDisplayRegion()
            dr.setSort(2)
            dr.setClearDepthActive(True)
            dr.setClearDepth(1.0)
            dr.setClearColorActive(False)
            dr.setDimensions(0, 0, 0, 0)
            self.depthClearer.append(dr)

        # When using hardware pcf, set the correct filter types
        dTex = self.shadowComputeTarget.getDepthTexture()

        if self.settings.useHardwarePCF:
            dTex.setMinfilter(Texture.FTShadow)
            dTex.setMagfilter(Texture.FTShadow)

        dTex.setWrapU(Texture.WMClamp)
        dTex.setWrapV(Texture.WMClamp)

    def _createDebugTexts(self):
        """ Creates a debug overlay if specified in the pipeline settings """
        self.lightsVisibleDebugText = None
        self.lightsUpdatedDebugText = None

        if self.settings.displayDebugStats:

            # FastText only is in my personal shared directory. It's not
            # in the git-repository. So to prevent errors, this try is here.
            try:
                from Code.FastText import FastText
                self.lightsVisibleDebugText = FastText(pos=Vec2(
                    Globals.base.getAspectRatio() - 0.1, 0.84), rightAligned=True, color=Vec3(1, 1, 0), size=0.036)
                self.lightsUpdatedDebugText = FastText(pos=Vec2(
                    Globals.base.getAspectRatio() - 0.1, 0.8), rightAligned=True, color=Vec3(1, 1, 0), size=0.036)

            except Exception, msg:
                self.debug(
                    "Overlay is disabled because FastText wasn't loaded")

    def _initArrays(self):
        """ Inits the light arrays which are passed to the shaders """

        # If you change this, don't forget to change it also in
        # Shader/Includes/Configuration.include!
        self.maxLights = {
            "PointLight": 16,
            "DirectionalLight": 1
        }

        # Max shadow casting lights
        self.maxShadowLights = {
            "PointLight": 16,
            "DirectionalLight": 1
        }

        self.maxTotalLights = 8

        for lightType, maxCount in self.maxShadowLights.items():
            self.maxLights[lightType + "Shadow"] = maxCount

        # Create array to store number of rendered lights this frame
        self.numRenderedLights = {}

        # Also create a PTAInt for every light type, which stores only the
        # light id, the lighting shader will then lookup the light in the
        # global lights array.
        self.renderedLightsArrays = {}
        for lightType, maxCount in self.maxLights.items():
            self.renderedLightsArrays[lightType] = PTAInt.emptyArray(maxCount)
            self.numRenderedLights[lightType] = PTAInt.emptyArray(1)

        for lightType, maxCount in self.maxShadowLights.items():
            self.renderedLightsArrays[
                lightType + "Shadow"] = PTAInt.emptyArray(maxCount)
            self.numRenderedLights[lightType + "Shadow"] = PTAInt.emptyArray(1)

    def setLightingComputator(self, shaderNode):
        """ Sets the render target which recieves the shaderinputs necessary to 
        compute the final lighting result """
        self.debug("Light computator is", shaderNode)
        self.lightingComputator = shaderNode

        self.allLightsArray.bindTo(shaderNode, "lights")
        self.allShadowsArray.bindTo(shaderNode, "shadowSources")

        for lightType, arrayData in self.renderedLightsArrays.items():
            shaderNode.setShaderInput("array" + lightType, arrayData)
            shaderNode.setShaderInput(
                "count" + lightType, self.numRenderedLights[lightType])

    def setLightingCuller(self, shaderNode):
        """ Sets the render target which recieves the shaderinputs necessary to 
        cull the lights and pass the result to the lighting computator"""
        self.debug("Light culler is", shaderNode)
        self.lightCuller = shaderNode

        self.allLightsArray.bindTo(shaderNode, "lights")

        # The culler needs the visible lights ids / counts as he has
        # to determine wheter a light is visible or not
        for lightType, arrayData in self.renderedLightsArrays.items():
            shaderNode.setShaderInput("array" + lightType, arrayData)
            shaderNode.setShaderInput(
                "count" + lightType, self.numRenderedLights[lightType])

    def getAtlasTex(self):
        """ Returns the shadow map atlas texture"""
        return self.shadowComputeTarget.getDepthTexture()

    def _queueShadowUpdate(self, source):
        """ Internal method to add a shadowSource to the list of queued updates """
        if source not in self.queuedShadowUpdates:
            self.queuedShadowUpdates.append(source)

    def addLight(self, light):
        """ Adds a light to the list of rendered lights.

        NOTICE: You have to set relevant properties like wheter the light
        casts shadows or the shadowmap resolution before calling this! 
        Otherwise it won't work (and maybe crash? I didn't test, 
        just DON'T DO IT!) """
        self.lights.append(light)
        light.attached = True

        sources = light.getShadowSources()

        # Check each shadow source
        for index, source in enumerate(sources):

            # Check for correct resolution
            tileSize = self.shadowAtlas.getTileSize()
            if source.resolution < tileSize or source.resolution % tileSize != 0:
                self.warn(
                    "The ShadowSource resolution has to be a multiple of the tile size (" + str(tileSize) + ")!")
                self.warn("Adjusting resolution to", tileSize)
                source.resolution = tileSize

            if source.resolution > self.shadowAtlas.getSize():
                self.warn(
                    "The ShadowSource resolution cannot be bigger than the atlas size (" + str(self.shadowAtlas.getSize()) + ")")
                self.warn("Adjusting resolution to", tileSize)
                source.resolution = tileSize

            if source not in self.shadowSources:
                self.shadowSources.append(source)

            source.setSourceIndex(self.shadowSources.index(source))
            light.setSourceIndex(index, source.getSourceIndex())

        index = self.lights.index(light)
        self.allLightsArray[index] = light

        light.queueUpdate()
        light.queueShadowUpdate()

    def removeLight(self):
        """ Removes a light. TODO """
        raise NotImplementedError()

    def debugReloadShader(self):
        """ Reloads all shaders. This also updates the camera state """
        self._createTagStates()

    def setCullBounds(self, bounds):
        """ Sets the current camera bounds used for light culling """
        self.cullBounds = bounds

    def updateLights(self):
        """ This is one of the two per-frame-tasks. See class description
        to see what it does """

        pstats_ProcessLights.start()

        # Reset light counts
        # We don't have to reset the data-vectors, as we overwrite them
        for key in self.numRenderedLights:
            self.numRenderedLights[key][0] = 0

        # Process each light
        for index, light in enumerate(self.lights):

            # Update light if required
            pstats_PerLightUpdates.start()
            if light.needsUpdate():
                light.performUpdate()
            pstats_PerLightUpdates.stop()

            # Perform culling

            pstats_CullLights.start()
            if not self.cullBounds.contains(light.getBounds()):
                continue
            pstats_CullLights.stop()

            # Queue shadow updates if necessary
            if light.hasShadows() and light.needsShadowUpdate():
                neededUpdates = light.performShadowUpdate()
                for update in neededUpdates:
                    self._queueShadowUpdate(update)

            # Add light to the correct list now
            lightTypeName = light.getTypeName()
            if light.hasShadows():
                lightTypeName += "Shadow"

            # Add to array and increment counter
            oldCount = self.numRenderedLights[lightTypeName][0]

            if oldCount >= self.maxLights[lightTypeName]:
                self.warn("Too many lights of type", lightTypeName,
                          "-> max is", self.maxLights[lightTypeName])
                continue

            arrayIndex = self.numRenderedLights[lightTypeName][0]
            self.numRenderedLights[lightTypeName][0] = oldCount + 1
            self.renderedLightsArrays[lightTypeName][arrayIndex] = index

        pstats_ProcessLights.stop()

        # Generate debug text
        if self.lightsVisibleDebugText is not None:
            renderedPL = "Point:" + \
                str(self.numRenderedLights["PointLight"][0])
            renderedPL_S = "Point:" + \
                str(self.numRenderedLights["PointLightShadow"][0])

            renderedDL = "Directional:" + \
                str(self.numRenderedLights["DirectionalLight"][0])
            renderedDL_S = "Directional:" + \
                str(self.numRenderedLights["DirectionalLightShadow"][0])


            self.lightsVisibleDebugText.setText(
                'Lights: ' + renderedPL + " / " + renderedDL + " Shadowed: " + renderedPL_S + " / " + renderedDL_S)

    def updateShadows(self):
        """ This is one of the two per-frame-tasks. See class description
        to see what it does """

        # Process shadows
        queuedUpdateLen = len(self.queuedShadowUpdates)

        # Compute shadow updates
        numUpdates = 0
        last = "[ "

        # First, disable all clearers
        for clearer in self.depthClearer:
            clearer.setActive(False)

        # When there are no updates, disable the buffer
        if len(self.queuedShadowUpdates) < 1:
            self.shadowComputeTarget.setActive(False)
            self.numShadowUpdatesPTA[0] = 0

        else:

            # Otherwise enable the buffer
            self.shadowComputeTarget.setActive(True)

            # Check each update in the queue
            for index, update in enumerate(self.queuedShadowUpdates):

                # We only process a limited number of shadow maps
                if numUpdates >= self.maxShadowUpdatesPerFrame:
                    break

                updateSize = update.getResolution()

                # assign position in atlas if not done yet
                if not update.hasAtlasPos():

                    storePos = self.shadowAtlas.reserveTiles(
                        updateSize, updateSize, update.getUid())

                    if not storePos:

                        # No space found, try to reduce resolution
                        self.warn(
                            "Could not find space for the shadow map of size", updateSize)
                        self.warn(
                            "The size will be reduced to", self.shadowAtlas.getTileSize())

                        updateSize = self.shadowAtlas.getTileSize()
                        update.setResolution(updateSize)
                        storePos = self.shadowAtlas.reserveTiles(
                            updateSize, updateSize, update.getUid())

                        if not storePos:
                            self.error(
                                "Still could not find a shadow atlas position, "
                                "the shadow atlas is completely full. "
                                "Either we reduce the resolution of existing shadow maps, "
                                "increase the shadow atlas resolution, "
                                "or crash the app. Guess what I decided to do :-P")

                            import sys
                            sys.exit(0)

                    update.assignAtlasPos(*storePos)

                update.update()

                # Store update in array
                indexInArray = self.shadowSources.index(update)
                self.allShadowsArray[indexInArray] = update
                self.updateShadowsArray[index] = update

                # Compute viewport & set depth clearer
                texScale = float(update.getResolution()) / \
                    float(self.shadowAtlas.getSize())

                atlasPos = update.getAtlasPos()
                left, right = atlasPos.x, (atlasPos.x + texScale)
                bottom, top = atlasPos.y, (atlasPos.y + texScale)
                self.depthClearer[numUpdates].setDimensions(
                    left, right, bottom, top)
                self.depthClearer[numUpdates].setActive(True)

                self.shadowComputeTarget.getInternalRegion().setDimensions(
                    numUpdates + 1, (atlasPos.x, atlasPos.x + texScale,
                                     atlasPos.y, atlasPos.y + texScale))
                numUpdates += 1

                # Finally, we can tell the update it's valid now.
                # Actually this is only true in one frame, but who cares?
                update.setValid()

                # Only add the uid to the output if the max updates
                # aren't too much. Otherwise we spam the screen :P
                if self.maxShadowUpdatesPerFrame <= 8:
                    last += str(update.getUid()) + " "

            # Remove all updates which got processed from the list
            for i in range(numUpdates):
                self.queuedShadowUpdates.remove(self.queuedShadowUpdates[0])

            self.numShadowUpdatesPTA[0] = numUpdates

        last += "]"

        # Generate debug text
        if self.lightsUpdatedDebugText is not None:
            self.lightsUpdatedDebugText.setText(
                'Queued Updates: ' + str(numUpdates) + "/" + str(queuedUpdateLen) + "/" + str(len(self.shadowSources)) + ", Last: " + last + ", Free Tiles: " + str(self.shadowAtlas.getFreeTileCount()) + "/" + str(self.shadowAtlas.getTotalTileCount()))

    # Main update
    def update(self):
        self.updateLights()
        self.updateShadows()