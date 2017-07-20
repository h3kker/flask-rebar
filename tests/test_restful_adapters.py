from flask_testing import TestCase
from flask import Blueprint
from flask import Flask
from flask import Response

from plangrid.flask_toolbox import Toolbox
from plangrid.flask_toolbox.restful_adapters import RestfulApiAdapter
from plangrid.flask_toolbox.http_errors import BadRequest


class TestRestfulApiAdapter(TestCase):
    def create_app(self):
        app = Flask(__name__)
        Toolbox(app)
        blueprint = Blueprint('test', __name__)
        adapter = RestfulApiAdapter(blueprint)
        adapter.add_resource(SimpleHandler, '/thing', ['GET', 'POST'])
        adapter.add_resource(EmptyResponseHandler, '/nothing', ['GET'])
        adapter.add_resource(KwargsHandler, '/exclaim/<string:word>', ['GET'])
        adapter.add_resource(ErrorThrowingHandler, '/error', ['GET'])
        adapter.add_resource(SingleReturnHandler, '/single_return', ['GET'])
        adapter.add_resource(FlaskResponseHandler, '/flask_response', ['GET'])
        app.register_blueprint(blueprint)
        return app

    def test_http_methods(self):
        resp = self.app.test_client().get('/thing')
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json, gen_test_dict())

        resp = self.app.test_client().post('/thing')
        self.assertEqual(resp.status_code, 201)
        self.assertEqual(resp.json, gen_test_dict())

        resp = self.app.test_client().delete('/thing')
        self.assertEqual(resp.status_code, 405)

        resp = self.app.test_client().get('/stuff')
        self.assertEqual(resp.status_code, 404)

    def test_empty_response(self):
        resp = self.app.test_client().get('/nothing')
        self.assertEqual(resp.data.decode('utf-8'), '')

    def test_kwargs(self):
        resp = self.app.test_client().get('/exclaim/hello')
        expected = gen_test_dict()
        expected['test'] = 'hello'
        self.assertEqual(resp.json, expected)

    def test_error_handler(self):
        resp = self.app.test_client().get('/error')
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.json, {'message': BadRequest.default_message})

    def test_single_return_method(self):
        resp = self.app.test_client().get('/single_return')
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json, gen_test_dict())

    def test_missing_http_method(self):
        blueprint = Blueprint('test', __name__)
        adapter = RestfulApiAdapter(blueprint)
        self.assertRaises(
            NotImplementedError,
            lambda: adapter.add_resource(SimpleHandler, '/thing', ['GET', 'DELETE'])
        )

    def test_endpoint_returns_a_flask_response(self):
        resp = self.app.test_client().get('/flask_response')
        self.assertEqual(resp.data.decode('utf-8'), '')


def gen_test_dict():
    return {'some': 'stuff', 'in_a': 'dict'}


class SimpleHandler(object):
    def get(self):
        return gen_test_dict(), 200

    def post(self):
        return gen_test_dict(), 201


class DictRespondingHandler(object):
    def get(self):
        return gen_test_dict(), 200


class EmptyResponseHandler(object):
    def get(self):
        return None, 204


class KwargsHandler(object):
    def get(self, word):
        val = gen_test_dict()
        val['test'] = word
        return val, 200


class ErrorThrowingHandler(object):
    def get(self):
        raise BadRequest()


class SingleReturnHandler(object):
    def get(self):
        return gen_test_dict()


class FlaskResponseHandler(object):
    def get(self):
        return Response()
