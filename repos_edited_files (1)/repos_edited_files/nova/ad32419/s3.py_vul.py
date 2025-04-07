# vim: tabstop=4 shiftwidth=4 softtabstop=4

# Copyright 2010 United States Government as represented by the
# Administrator of the National Aeronautics and Space Administration.
# All Rights Reserved.
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.

"""Proxy AMI-related calls from cloud controller to objectstore service."""

import binascii
import os
import shutil
import tarfile
import tempfile
from xml.etree import ElementTree

import boto.s3.connection
import eventlet

from nova import crypto
import nova.db.api
from nova import exception
from nova import flags
from nova import image
from nova import log as logging
from nova import utils
from nova.api.ec2 import ec2utils


LOG = logging.getLogger("nova.image.s3")
FLAGS = flags.FLAGS
flags.DEFINE_string('image_decryption_dir', '/tmp',
                    'parent dir for tempdir used for image decryption')
flags.DEFINE_string('s3_access_key', 'notchecked',
                    'access key to use for s3 server for images')
flags.DEFINE_string('s3_secret_key', 'notchecked',
                    'secret key to use for s3 server for images')


class S3ImageService(object):
    """Wraps an existing image service to support s3 based register."""

    def __init__(self, service=None, *args, **kwargs):
        self.service = service or image.get_default_image_service()
        self.service.__init__(*args, **kwargs)

    def get_image_uuid(self, context, image_id):
        return nova.db.api.s3_image_get(context, image_id)['uuid']

    def get_image_id(self, context, image_uuid):
        return nova.db.api.s3_image_get_by_uuid(context, image_uuid)['id']

    def _create_image_id(self, context, image_uuid):
        return nova.db.api.s3_image_create(context, image_uuid)['id']

    def _translate_uuids_to_ids(self, context, images):
        return [self._translate_uuid_to_id(context, img) for img in images]

    def _translate_uuid_to_id(self, context, image):
        def _find_or_create(image_uuid):
            if image_uuid is None:
                return
            try:
                return self.get_image_id(context, image_uuid)
            except exception.NotFound:
                return self._create_image_id(context, image_uuid)

        image_copy = image.copy()

        try:
            image_id = image_copy['id']
        except KeyError:
            pass
        else:
            image_copy['id'] = _find_or_create(image_id)

        for prop in ['kernel_id', 'ramdisk_id']:
            try:
                image_uuid = image_copy['properties'][prop]
            except (KeyError, ValueError):
                pass
            else:
                image_copy['properties'][prop] = _find_or_create(image_uuid)

        return image_copy

    def create(self, context, metadata, data=None):
        """Create an image.

        metadata['properties'] should contain image_location.

        """
        image = self._s3_create(context, metadata)
        return image

    def delete(self, context, image_id):
        image_uuid = self.get_image_uuid(context, image_id)
        self.service.delete(context, image_uuid)

    def update(self, context, image_id, metadata, data=None):
        image_uuid = self.get_image_uuid(context, image_id)
        image = self.service.update(context, image_uuid, metadata, data)
        return self._translate_uuid_to_id(context, image)

    def index(self, context):
        #NOTE(bcwaldon): sort asc to make sure we assign lower ids
        # to older images
        images = self.service.index(context, sort_dir='asc')
        return self._translate_uuids_to_ids(context, images)

    def detail(self, context):
        #NOTE(bcwaldon): sort asc to make sure we assign lower ids
        # to older images
        images = self.service.detail(context, sort_dir='asc')
        return self._translate_uuids_to_ids(context, images)

    def show(self, context, image_id):
        image_uuid = self.get_image_uuid(context, image_id)
        image = self.service.show(context, image_uuid)
        return self._translate_uuid_to_id(context, image)

    def show_by_name(self, context, name):
        image = self.service.show_by_name(context, name)
        return self._translate_uuid_to_id(context, image)

    def get(self, context, image_id):
        image_uuid = self.get_image_uuid(context, image_id)
        return self.get(self, context, image_uuid)

    @staticmethod
    def _conn(context):
        # NOTE(vish): access and secret keys for s3 server are not
        #             checked in nova-objectstore
        access = FLAGS.s3_access_key
        secret = FLAGS.s3_secret_key
        calling = boto.s3.connection.OrdinaryCallingFormat()
        return boto.s3.connection.S3Connection(aws_access_key_id=access,
                                               aws_secret_access_key=secret,
                                               is_secure=False,
                                               calling_format=calling,
                                               port=FLAGS.s3_port,
                                               host=FLAGS.s3_host)

    @staticmethod
    def _download_file(bucket, filename, local_dir):
        key = bucket.get_key(filename)
        local_filename = os.path.join(local_dir, filename)
        key.get_contents_to_filename(local_filename)
        return local_filename

    def _s3_parse_manifest(self, context, metadata, manifest):
        manifest = ElementTree.fromstring(manifest)
        image_format = 'ami'
        image_type = 'machine'

        try:
            kernel_id = manifest.find('machine_configuration/kernel_id').text
            if kernel_id == 'true':
                image_format = 'aki'
                image_type = 'kernel'
                kernel_id = None
        except Exception:
            kernel_id = None

        try:
            ramdisk_id = manifest.find('machine_configuration/ramdisk_id').text
            if ramdisk_id == 'true':
                image_format = 'ari'
                image_type = 'ramdisk'
                ramdisk_id = None
        except Exception:
            ramdisk_id = None

        try:
            arch = manifest.find('machine_configuration/architecture').text
        except Exception:
            arch = 'x86_64'

        # NOTE(yamahata):
        # EC2 ec2-budlne-image --block-device-mapping accepts
        # <virtual name>=<device name> where
        # virtual name = {ami, root, swap, ephemeral<N>}
        #                where N is no negative integer
        # device name = the device name seen by guest kernel.
        # They are converted into
        # block_device_mapping/mapping/{virtual, device}
        #
        # Do NOT confuse this with ec2-register's block device mapping
        # argument.
        mappings = []
        try:
            block_device_mapping = manifest.findall('machine_configuration/'
                                                    'block_device_mapping/'
                                                    'mapping')
            for bdm in block_device_mapping:
                mappings.append({'virtual': bdm.find('virtual').text,
                                 'device': bdm.find('device').text})
        except Exception:
            mappings = []

        properties = metadata['properties']
        properties['project_id'] = context.project_id
        properties['architecture'] = arch

        def _translate_dependent_image_id(image_key, image_id):
            image_id = ec2utils.ec2_id_to_id(image_id)
            image_uuid = self.get_image_uuid(context, image_id)
            properties['image_id'] = image_uuid

        if kernel_id:
            _translate_dependent_image_id('kernel_id', kernel_id)

        if ramdisk_id:
            _translate_dependent_image_id('ramdisk_id', ramdisk_id)

        if mappings:
            properties['mappings'] = mappings

        metadata.update({'disk_format': image_format,
                         'container_format': image_format,
                         'status': 'queued',
                         'is_public': False,
                         'properties': properties})
        metadata['properties']['image_state'] = 'pending'

        #TODO(bcwaldon): right now, this removes user-defined ids.
        # We need to re-enable this.
        image_id = metadata.pop('id', None)

        image = self.service.create(context, metadata)

        # extract the new uuid and generate an int id to present back to user
        image_uuid = image['id']
        image['id'] = self._create_image_id(context, image_uuid)

        # return image_uuid so the caller can still make use of image_service
        return manifest, image, image_uuid

    def _s3_create(self, context, metadata):
        """Gets a manifext from s3 and makes an image."""

        image_path = tempfile.mkdtemp(dir=FLAGS.image_decryption_dir)

        image_location = metadata['properties']['image_location']
        bucket_name = image_location.split('/')[0]
        manifest_path = image_location[len(bucket_name) + 1:]
        bucket = self._conn(context).get_bucket(bucket_name)
        key = bucket.get_key(manifest_path)
        manifest = key.get_contents_as_string()

        manifest, image, image_uuid = self._s3_parse_manifest(context,
                                                              metadata,
                                                              manifest)

        def delayed_create():
            """This handles the fetching and decrypting of the part files."""
            log_vars = {'image_location': image_location,
                        'image_path': image_path}
            metadata['properties']['image_state'] = 'downloading'
            self.service.update(context, image_uuid, metadata)

            try:
                parts = []
                elements = manifest.find('image').getiterator('filename')
                for fn_element in elements:
                    part = self._download_file(bucket,
                                               fn_element.text,
                                               image_path)
                    parts.append(part)

                # NOTE(vish): this may be suboptimal, should we use cat?
                enc_filename = os.path.join(image_path, 'image.encrypted')
                with open(enc_filename, 'w') as combined:
                    for filename in parts:
                        with open(filename) as part:
                            shutil.copyfileobj(part, combined)

            except Exception:
                LOG.exception(_("Failed to download %(image_location)s "
                                "to %(image_path)s"), log_vars)
                metadata['properties']['image_state'] = 'failed_download'
                self.service.update(context, image_uuid, metadata)
                return

            metadata['properties']['image_state'] = 'decrypting'
            self.service.update(context, image_uuid, metadata)

            try:
                hex_key = manifest.find('image/ec2_encrypted_key').text
                encrypted_key = binascii.a2b_hex(hex_key)
                hex_iv = manifest.find('image/ec2_encrypted_iv').text
                encrypted_iv = binascii.a2b_hex(hex_iv)

                # FIXME(vish): grab key from common service so this can run on
                #              any host.
                cloud_pk = crypto.key_path(context.project_id)

                dec_filename = os.path.join(image_path, 'image.tar.gz')
                self._decrypt_image(enc_filename, encrypted_key,
                                    encrypted_iv, cloud_pk,
                                    dec_filename)
            except Exception:
                LOG.exception(_("Failed to decrypt %(image_location)s "
                                "to %(image_path)s"), log_vars)
                metadata['properties']['image_state'] = 'failed_decrypt'
                self.service.update(context, image_uuid, metadata)
                return

            metadata['properties']['image_state'] = 'untarring'
            self.service.update(context, image_uuid, metadata)

            try:
                unz_filename = self._untarzip_image(image_path, dec_filename)
            except Exception:
                LOG.exception(_("Failed to untar %(image_location)s "
                                "to %(image_path)s"), log_vars)
                metadata['properties']['image_state'] = 'failed_untar'
                self.service.update(context, image_uuid, metadata)
                return

            metadata['properties']['image_state'] = 'uploading'
            self.service.update(context, image_uuid, metadata)
            try:
                with open(unz_filename) as image_file:
                    self.service.update(context, image_uuid,
                                        metadata, image_file)
            except Exception:
                LOG.exception(_("Failed to upload %(image_location)s "
                                "to %(image_path)s"), log_vars)
                metadata['properties']['image_state'] = 'failed_upload'
                self.service.update(context, image_uuid, metadata)
                return

            metadata['properties']['image_state'] = 'available'
            metadata['status'] = 'active'
            self.service.update(context, image_uuid, metadata)

            shutil.rmtree(image_path)

        eventlet.spawn_n(delayed_create)

        return image

    @staticmethod
    def _decrypt_image(encrypted_filename, encrypted_key, encrypted_iv,
                       cloud_private_key, decrypted_filename):
        key, err = utils.execute('openssl',
                                 'rsautl',
                                 '-decrypt',
                                 '-inkey', '%s' % cloud_private_key,
                                 process_input=encrypted_key,
                                 check_exit_code=False)
        if err:
            raise exception.Error(_('Failed to decrypt private key: %s')
                                  % err)
        iv, err = utils.execute('openssl',
                                'rsautl',
                                '-decrypt',
                                '-inkey', '%s' % cloud_private_key,
                                process_input=encrypted_iv,
                                check_exit_code=False)
        if err:
            raise exception.Error(_('Failed to decrypt initialization '
                                    'vector: %s') % err)

        _out, err = utils.execute('openssl', 'enc',
                                  '-d', '-aes-128-cbc',
                                  '-in', '%s' % (encrypted_filename,),
                                  '-K', '%s' % (key,),
                                  '-iv', '%s' % (iv,),
                                  '-out', '%s' % (decrypted_filename,),
                                  check_exit_code=False)
        if err:
            raise exception.Error(_('Failed to decrypt image file '
                                    '%(image_file)s: %(err)s') %
                                    {'image_file': encrypted_filename,
                                     'err': err})

    @staticmethod
    def _untarzip_image(path, filename):
        tar_file = tarfile.open(filename, 'r|gz')
        tar_file.extractall(path)
        image_file = tar_file.getnames()[0]
        tar_file.close()
        return os.path.join(path, image_file)
